import os
import io
import time
import cv2
from datetime import datetime
from datetime import timedelta
from pathlib import Path
import subprocess
import tempfile
import fcntl
import errno
import logging

from .keogram import KeogramGenerator
from .starTrails import StarTrailGenerator

from .exceptions import InsufficentData

from .flask import db
from .flask.miscDb import miscDb

from sqlalchemy.orm.exc import NoResultFound

from multiprocessing import Process
import queue
#from threading import Thread

logger = logging.getLogger('indi_allsky')


class VideoWorker(Process):

    video_lockfile = '/tmp/timelapse_video.lock'


    def __init__(self, idx, config, video_q, upload_q):
        super(VideoWorker, self).__init__()

        #self.threadID = idx
        self.name = 'VideoWorker{0:03d}'.format(idx)

        self.config = config
        self.video_q = video_q
        self.upload_q = upload_q

        self._miscDb = miscDb(self.config)

        self.f_lock = None


    def run(self):
        while True:
            time.sleep(1.0)  # sleep every loop

            try:
                v_dict = self.video_q.get_nowait()
            except queue.Empty:
                continue

            if v_dict.get('stop'):
                return

            try:
                self._getLock()  # get lock to prevent multiple videos from being concurrently generated
            except BlockingIOError as e:
                if e.errno == errno.EAGAIN:
                    logger.error('Failed to get exclusive lock: %s', str(e))
                    return


            timespec = v_dict['timespec']
            img_folder = v_dict['img_folder']
            timeofday = v_dict['timeofday']
            camera_id = v_dict['camera_id']
            video = v_dict.get('video', True)
            keogram = v_dict.get('keogram', True)
            #startrail = v_dict.get('startrail', True)
            expireData = v_dict.get('expireData', False)


            if not img_folder.exists():
                logger.error('Image folder does not exist: %s', img_folder)
                continue


            if expireData:
                self.expireData(img_folder)


            if video:
                self.generateVideo(timespec, img_folder, timeofday, camera_id)


            if keogram:
                self.generateKeogramStarTrails(timespec, img_folder, timeofday, camera_id)


            self._releaseLock()



    def generateVideo(self, timespec, img_folder, timeofday, camera_id):
        from .flask.models import IndiAllSkyDbCameraTable
        from .flask.models import IndiAllSkyDbImageTable
        from .flask.models import IndiAllSkyDbVideoTable


        try:
            d_dayDate = datetime.strptime(timespec, '%Y%m%d').date()
        except ValueError:
            logger.error('Invalid time spec')
            return


        if timeofday == 'night':
            night = True
        else:
            night = False


        video_file = img_folder.parent.joinpath('allsky-timelapse_ccd{0:d}_{1:s}_{2:s}.mp4'.format(camera_id, timespec, timeofday))

        if video_file.exists():
            logger.warning('Video is already generated: %s', video_file)
            return


        try:
            # delete old video entry if it exists
            video_entry = IndiAllSkyDbVideoTable.query\
                .filter(IndiAllSkyDbVideoTable.filename == str(video_file))\
                .one()

            logger.warning('Removing orphaned video db entry')
            db.session.delete(video_entry)
            db.session.commit()
        except NoResultFound:
            pass


        seqfolder = tempfile.TemporaryDirectory()
        p_seqfolder = Path(seqfolder.name)


        # find all files
        timelapse_files_entries = IndiAllSkyDbImageTable.query\
            .join(IndiAllSkyDbImageTable.camera)\
            .filter(IndiAllSkyDbCameraTable.id == camera_id)\
            .filter(IndiAllSkyDbImageTable.dayDate == d_dayDate)\
            .filter(IndiAllSkyDbImageTable.night == night)\
            .order_by(IndiAllSkyDbImageTable.createDate.asc())


        logger.info('Found %d images for timelapse', timelapse_files_entries.count())

        timelapse_files = list()
        for entry in timelapse_files_entries:
            p_entry = Path(entry.filename)

            if not p_entry.exists():
                logger.error('File not found: %s', p_entry)
                continue

            if p_entry.stat().st_size == 0:
                continue

            timelapse_files.append(p_entry)


        for i, f in enumerate(timelapse_files):
            p_symlink = p_seqfolder.joinpath('{0:05d}.{1:s}'.format(i, self.config['IMAGE_FILE_TYPE']))
            p_symlink.symlink_to(f)


        start = time.time()

        cmd = [
            'ffmpeg',
            '-y',
            '-f', 'image2',
            '-r', '{0:d}'.format(self.config['FFMPEG_FRAMERATE']),
            '-i', '{0:s}/%05d.{1:s}'.format(str(p_seqfolder), self.config['IMAGE_FILE_TYPE']),
            '-vcodec', 'libx264',
            '-b:v', '{0:s}'.format(self.config['FFMPEG_BITRATE']),
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '{0:s}'.format(str(video_file)),
        ]

        ffmpeg_subproc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=lambda: os.nice(19),
        )

        elapsed_s = time.time() - start
        logger.info('Timelapse generated in %0.4f s', elapsed_s)

        logger.info('FFMPEG output: %s', ffmpeg_subproc.stdout)

        # delete all existing symlinks and sequence folder
        seqfolder.cleanup()


        video_entry = self._miscDb.addVideo(
            video_file,
            camera_id,
            d_dayDate,
            timeofday,
        )

        ### Upload ###
        self.uploadVideo(video_file)

        self._miscDb.addUploadedFlag(video_entry)



    def uploadVideo(self, video_file):
        ### Upload video
        if not self.config['FILETRANSFER']['UPLOAD_VIDEO']:
            logger.warning('Video uploading disabled')
            return

        remote_path = Path(self.config['FILETRANSFER']['REMOTE_VIDEO_FOLDER'])
        remote_file = remote_path.joinpath(video_file.name)

        # tell worker to upload file
        self.upload_q.put({
            'local_file' : video_file,
            'remote_file' : remote_file,
        })


    def generateKeogramStarTrails(self, timespec, img_folder, timeofday, camera_id):
        from .flask.models import IndiAllSkyDbCameraTable
        from .flask.models import IndiAllSkyDbImageTable
        from .flask.models import IndiAllSkyDbKeogramTable
        from .flask.models import IndiAllSkyDbStarTrailsTable


        try:
            d_dayDate = datetime.strptime(timespec, '%Y%m%d').date()
        except ValueError:
            logger.error('Invalid time spec')
            return


        if timeofday == 'night':
            night = True
        else:
            night = False



        keogram_file = img_folder.parent.joinpath('allsky-keogram_ccd{0:d}_{1:s}_{2:s}.jpg'.format(camera_id, timespec, timeofday))
        startrail_file = img_folder.parent.joinpath('allsky-startrail_ccd{0:d}_{1:s}_{2:s}.jpg'.format(camera_id, timespec, timeofday))

        if keogram_file.exists():
            logger.warning('Keogram is already generated: %s', keogram_file)
            return

        if startrail_file.exists():
            logger.warning('Star trail is already generated: %s', startrail_file)
            return


        try:
            # delete old keogram entry if it exists
            keogram_entry = IndiAllSkyDbKeogramTable.query\
                .filter(IndiAllSkyDbKeogramTable.filename == str(keogram_file))\
                .one()

            logger.warning('Removing orphaned keogram db entry')
            db.session.delete(keogram_entry)
            db.session.commit()
        except NoResultFound:
            pass


        try:
            # delete old star trail entry if it exists
            startrail_entry = IndiAllSkyDbStarTrailsTable.query\
                .filter(IndiAllSkyDbStarTrailsTable.filename == str(startrail_file))\
                .one()

            logger.warning('Removing orphaned star trail db entry')
            db.session.delete(startrail_entry)
            db.session.commit()
        except NoResultFound:
            pass


        # find all files
        files_entries = IndiAllSkyDbImageTable.query\
            .join(IndiAllSkyDbImageTable.camera)\
            .filter(IndiAllSkyDbCameraTable.id == camera_id)\
            .filter(IndiAllSkyDbImageTable.dayDate == d_dayDate)\
            .filter(IndiAllSkyDbImageTable.night == night)\
            .order_by(IndiAllSkyDbImageTable.createDate.asc())


        image_count = files_entries.count()
        logger.info('Found %d images for keogram/star trails', image_count)


        processing_start = time.time()

        kg = KeogramGenerator(self.config)
        kg.angle = self.config['KEOGRAM_ANGLE']
        kg.h_scale_factor = self.config['KEOGRAM_H_SCALE']
        kg.v_scale_factor = self.config['KEOGRAM_V_SCALE']


        stg = StarTrailGenerator(self.config)
        stg.max_brightness = self.config['STARTRAILS_MAX_ADU']
        stg.mask_threshold = self.config['STARTRAILS_MASK_THOLD']
        stg.pixel_cutoff_threshold = self.config['STARTRAILS_PIXEL_THOLD']


        # Files are presorted from the DB
        for i, entry in enumerate(files_entries):
            if i % 100 == 0:
                logger.info('Processed %d of %d images', i, image_count)

            p_entry = Path(entry.filename)

            if not p_entry.exists():
                logger.error('File not found: %s', p_entry)
                continue

            if p_entry.stat().st_size == 0:
                continue

            #logger.info('Reading file: %s', p_entry)
            image = cv2.imread(str(p_entry), cv2.IMREAD_UNCHANGED)

            if isinstance(image, type(None)):
                logger.error('Unable to read %s', p_entry)
                continue

            kg.processImage(p_entry, image)

            if night:
                stg.processImage(p_entry, image)


        kg.finalize(keogram_file)

        if night:
            try:
                stg.finalize(startrail_file)
            except InsufficentData as e:
                logger.error('Error generating star trail: %s', str(e))


        processing_elapsed_s = time.time() - processing_start
        logger.warning('Total keogram/star trail processing in %0.1f s', processing_elapsed_s)


        if keogram_file.exists():
            keogram_entry = self._miscDb.addKeogram(
                keogram_file,
                camera_id,
                d_dayDate,
                timeofday,
            )

            self.uploadKeogram(keogram_file)
            self._miscDb.addUploadedFlag(keogram_entry)


        if night and startrail_file.exists():
            startrail_entry = self._miscDb.addStarTrail(
                startrail_file,
                camera_id,
                d_dayDate,
                timeofday=timeofday,
            )

            self.uploadStarTrail(startrail_file)
            self._miscDb.addUploadedFlag(startrail_entry)


    def uploadKeogram(self, keogram_file):
        ### Upload video
        if not self.config['FILETRANSFER'].get('UPLOAD_KEOGRAM'):
            logger.warning('Keogram uploading disabled')
            return

        remote_path = Path(self.config['FILETRANSFER']['REMOTE_KEOGRAM_FOLDER'])
        remote_file = remote_path.joinpath(keogram_file.name)

        # tell worker to upload file
        self.upload_q.put({
            'local_file' : keogram_file,
            'remote_file' : remote_file,
        })


    def uploadStarTrail(self, startrail_file):
        if not self.config['FILETRANSFER'].get('UPLOAD_STARTRAIL'):
            logger.warning('Star trail uploading disabled')
            return

        remote_path = Path(self.config['FILETRANSFER']['REMOTE_STARTRAIL_FOLDER'])
        remote_file = remote_path.joinpath(startrail_file.name)

        # tell worker to upload file
        self.upload_q.put({
            'local_file' : startrail_file,
            'remote_file' : remote_file,
        })


    def expireData(self, img_folder):
        from .flask.models import IndiAllSkyDbImageTable


        # Old image files need to be pruned
        cutoff_age_images = datetime.now() - timedelta(days=self.config['IMAGE_EXPIRE_DAYS'])

        old_images = IndiAllSkyDbImageTable.query.filter(IndiAllSkyDbImageTable.createDate < cutoff_age_images)


        logger.warning('Found %d expired images to delete', old_images.count())
        for file_entry in old_images:
            #logger.info('Removing old image: %s', file_entry.filename)

            file_p = Path(file_entry.filename)

            try:
                file_p.unlink()
            except OSError as e:
                logger.error('Cannot remove file: %s', str(e))
                continue
            except FileNotFoundError as e:
                logger.warning('File already removed: %s', str(e))


        old_images.delete()  # mass delete
        db.session.commit()


        # Remove empty folders
        dir_list = list()
        self.getFolderFolders(img_folder, dir_list)

        empty_dirs = filter(lambda p: not any(p.iterdir()), dir_list)
        for d in empty_dirs:
            logger.info('Removing empty directory: %s', d)

            try:
                d.rmdir()
            except OSError as e:
                logger.error('Cannot remove folder: %s', str(e))


    def getFolderFilesByExt(self, folder, file_list, extension_list=None):
        if not extension_list:
            extension_list = [self.config['IMAGE_FILE_TYPE']]

        logger.info('Searching for image files in %s', folder)

        dot_extension_list = ['.{0:s}'.format(e) for e in extension_list]

        for item in Path(folder).iterdir():
            if item.is_file() and item.suffix in dot_extension_list:
                file_list.append(item)
            elif item.is_dir():
                self.getFolderFilesByExt(item, file_list, extension_list=extension_list)  # recursion


    def getFolderFolders(self, folder, dir_list):
        for item in Path(folder).iterdir():
            if item.is_dir():
                dir_list.append(item)
                self.getFolderFolders(item, dir_list)  # recursion


    def _getLock(self):
        logger.info('Get exclusive lock to generate video')
        lock_p = Path(self.video_lockfile)

        if not lock_p.is_file():
            f_lock = io.open(str(lock_p), 'w+')
            f_lock.close()
            lock_p.chmod(0o644)

        self.f_lock = io.open(str(lock_p), 'w+')
        fcntl.flock(self.f_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Exclusive, non-blocking lock


    def _releaseLock(self):
        logger.info('Release exclusive lock')
        fcntl.flock(self.f_lock, fcntl.LOCK_UN)
        self.f_lock.close()

