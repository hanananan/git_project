#!/usr/bin/env python3
""" Alignments file functions for reading, writing and manipulating the data stored in a
serialized alignments file. """

import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, TYPE_CHECKING, Union

import numpy as np

from lib.serializer import get_serializer, get_serializer_from_filename
from lib.utils import FaceswapError

if sys.version_info < (3, 8):
    from typing_extensions import TypedDict
else:
    from typing import TypedDict

if TYPE_CHECKING:
    from .aligned_face import CenteringType

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name
_VERSION = 2.2
# VERSION TRACKING
# 1.0 - Never really existed. Basically any alignments file prior to version 2.0
# 2.0 - Implementation of full head extract. Any alignments version below this will have used
#       legacy extract
# 2.1 - Alignments data to extracted face PNG header. SHA1 hashes of faces no longer calculated
#       or stored in alignments file
# 2.2 - Add support for differently centered masks (i.e. not all masks stored as face centering)


# TODO Convert these to Dataclasses
class MaskAlignmentsFileDict(TypedDict):
    """ Typed Dictionary for storing Masks. """
    mask: bytes
    affine_matrix: Union[List[float], np.ndarray]
    interpolator: int
    stored_size: int
    stored_centering: "CenteringType"


class PNGHeaderAlignmentsDict(TypedDict):
    """ Base Dictionary for storing Alignment Information in Alignments files and PNG Headers. """
    x: int
    y: int
    w: int
    h: int
    landmarks_xy: Union[List[float], np.ndarray]
    mask: Dict[str, MaskAlignmentsFileDict]


class AlignmentFileDict(PNGHeaderAlignmentsDict):
    """ Typed Dictionary for storing Alignment Information in alignments files. """
    thumb: Optional[np.ndarray]


class PNGHeaderSourceDict(TypedDict):
    """ Dictionary for storing additional meta information in PNG headers """
    alignments_version: float
    original_filename: str
    face_index: int
    source_filename: str
    source_is_video: bool


class PNGHeaderDict(TypedDict):
    """ Dictionary for storing all alignment and meta information in PNG Headers """
    alignments: PNGHeaderAlignmentsDict
    source: PNGHeaderSourceDict


class Alignments():
    """ The alignments file is a custom serialized ``.fsa`` file that holds information for each
    frame for a video or series of images.

    Specifically, it holds a list of faces that appear in each frame. Each face contains
    information detailing their detected bounding box location within the frame, the 68 point
    facial landmarks and any masks that have been extracted.

    Additionally it can also hold video meta information (timestamp and whether a frame is a
    key frame.)

    Parameters
    ----------
    folder: str
        The folder that contains the alignments ``.fsa`` file
    filename: str, optional
        The filename of the ``.fsa`` alignments file. If not provided then the given folder will be
        checked for a default alignments file filename. Default: "alignments"
    """
    def __init__(self, folder, filename="alignments"):
        logger.debug("Initializing %s: (folder: '%s', filename: '%s')",
                     self.__class__.__name__, folder, filename)
        self._version = _VERSION
        self._serializer = get_serializer("compressed")
        self._file = self._get_location(folder, filename)
        self._meta = None
        self._data = self._load()
        self._update_legacy()
        self._hashes_to_frame = {}
        self._hashes_to_alignment = {}
        self._thumbnails = Thumbnails(self)
        logger.debug("Initialized %s", self.__class__.__name__)

    # << PROPERTIES >> #

    @property
    def frames_count(self):
        """ int: The number of frames that appear in the alignments :attr:`data`. """
        retval = len(self._data)
        logger.trace(retval)
        return retval

    @property
    def faces_count(self):
        """ int: The total number of faces that appear in the alignments :attr:`data`. """
        retval = sum(len(val["faces"]) for val in self._data.values())
        logger.trace(retval)
        return retval

    @property
    def file(self):
        """ str: The full path to the currently loaded alignments file. """
        return self._file

    @property
    def data(self):
        """ dict: The loaded alignments :attr:`file` in dictionary form. """
        return self._data

    @property
    def have_alignments_file(self):
        """ bool: ``True`` if an alignments file exists at location :attr:`file` otherwise
        ``False``. """
        retval = os.path.exists(self._file)
        logger.trace(retval)
        return retval

    @property
    def hashes_to_frame(self):
        """ dict: The SHA1 hash of the face mapped to the frame(s) and face index within the frame
        that the hash corresponds to. The structure of the dictionary is:

        {**SHA1_hash** (`str`): {**filename** (`str`): **face_index** (`int`)}}.

        Notes
        -----
        This method is depractated and exists purely for updating legacy hash based alignments
        to new png header storage in :class:`lib.align.update_legacy_png_header`.

        The first time this property is referenced, the dictionary will be created and cached.
        Subsequent references will be made to this cached dictionary.
        """
        if not self._hashes_to_frame:
            logger.debug("Generating hashes to frame")
            for frame_name, val in self._data.items():
                for idx, face in enumerate(val["faces"]):
                    self._hashes_to_frame.setdefault(face["hash"], {})[frame_name] = idx
        return self._hashes_to_frame

    @property
    def hashes_to_alignment(self):
        """ dict: The SHA1 hash of the face mapped to the alignment for the face that the hash
        corresponds to. The structure of the dictionary is:

        Notes
        -----
        This method is depractated and exists purely for updating legacy hash based alignments
        to new png header storage in :class:`lib.align.update_legacy_png_header`.

        The first time this property is referenced, the dictionary will be created and cached.
        Subsequent references will be made to this cached dictionary.
        """
        if not self._hashes_to_alignment:
            logger.debug("Generating hashes to alignment")
            self._hashes_to_alignment = {face["hash"]: face
                                         for val in self._data.values()
                                         for face in val["faces"]}
        return self._hashes_to_alignment

    @property
    def mask_summary(self):
        """ dict: The mask type names stored in the alignments :attr:`data` as key with the number
        of faces which possess the mask type as value. """
        masks = {}
        for val in self._data.values():
            for face in val["faces"]:
                if face.get("mask", None) is None:
                    masks["none"] = masks.get("none", 0) + 1
                for key in face.get("mask", {}):
                    masks[key] = masks.get(key, 0) + 1
        return masks

    @property
    def video_meta_data(self):
        """ dict: The frame meta data stored in the alignments file. If data does not exist in the
        alignments file then ``None`` is returned for each Key """
        retval = dict(pts_time=None, keyframes=None)
        pts_time = []
        keyframes = []
        for idx, key in enumerate(sorted(self.data)):
            if "video_meta" not in self.data[key]:
                return retval
            meta = self.data[key]["video_meta"]
            pts_time.append(meta["pts_time"])
            if meta["keyframe"]:
                keyframes.append(idx)
        retval = dict(pts_time=pts_time, keyframes=keyframes)
        return retval

    @property
    def thumbnails(self):
        """ :class:`~lib.align.Thumbnails`: The low resolution thumbnail images that exist
        within the alignments file """
        return self._thumbnails

    @property
    def version(self):
        """ float: The alignments file version number. """
        return self._version

    # << INIT FUNCTIONS >> #

    def _get_location(self, folder, filename):
        """ Obtains the location of an alignments file.

        If a legacy alignments file is provided/discovered, then the alignments file will be
        updated to the custom ``.fsa`` format and saved.

        Parameters
        ----------
        folder: str
            The folder that the alignments file is located in
        filename: str
            The filename of the alignments file

        Returns
        -------
        str
            The full path to the alignments file
        """
        logger.debug("Getting location: (folder: '%s', filename: '%s')", folder, filename)
        noext_name, extension = os.path.splitext(filename)
        if extension in (".json", ".p", ".pickle", ".yaml", ".yml"):
            # Reformat legacy alignments file
            filename = self._update_file_format(folder, filename)
            logger.debug("Updated legacy alignments. New filename: '%s'", filename)
        if extension[1:] == self._serializer.file_extension:
            logger.debug("Valid Alignments filename provided: '%s'", filename)
        else:
            filename = f"{noext_name}.{self._serializer.file_extension}"
            logger.debug("File extension set from serializer: '%s'",
                         self._serializer.file_extension)
        location = os.path.join(str(folder), filename)
        if not os.path.exists(location):
            # Test for old format alignments files and reformat if they exist. This will be
            # executed if an alignments file has not been explicitly provided therefore it will not
            # have been picked up in the extension test
            self._test_for_legacy(location)
        logger.verbose("Alignments filepath: '%s'", location)
        return location

    # << I/O >> #

    def _load(self):
        """ Load the alignments data from the serialized alignments :attr:`file`.

        Populates :attr:`_meta` with the alignment file's meta information as well as returning
        the serialized data.

        Returns
        -------
        dict:
            The loaded alignments data
        """
        logger.debug("Loading alignments")
        if not self.have_alignments_file:
            raise FaceswapError(f"Error: Alignments file not found at {self._file}")

        logger.info("Reading alignments from: '%s'", self._file)
        data = self._serializer.load(self._file)
        self._meta = data.get("__meta__", dict(version=1.0))
        self._version = self._meta["version"]
        data = data.get("__data__", data)
        logger.debug("Loaded alignments")
        return data

    def save(self):
        """ Write the contents of :attr:`data` and :attr:`_meta` to a serialized ``.fsa`` file at
        the location :attr:`file`. """
        logger.debug("Saving alignments")
        logger.info("Writing alignments to: '%s'", self._file)
        data = dict(__meta__=dict(version=self._version),
                    __data__=self._data)
        self._serializer.save(self._file, data)
        logger.debug("Saved alignments")

    def backup(self):
        """ Create a backup copy of the alignments :attr:`file`.

        Creates a copy of the serialized alignments :attr:`file` appending a
        timestamp onto the end of the file name and storing in the same folder as
        the original :attr:`file`.
        """
        logger.debug("Backing up alignments")
        if not os.path.isfile(self._file):
            logger.debug("No alignments to back up")
            return
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        src = self._file
        split = os.path.splitext(src)
        dst = split[0] + "_" + now + split[1]
        logger.info("Backing up original alignments to '%s'", dst)
        os.rename(src, dst)
        logger.debug("Backed up alignments")

    def save_video_meta_data(self, pts_time, keyframes):
        """ Save video meta data to the alignments file.

        If the alignments file does not have an entry for every frame (e.g. if Extract Every N
        was used) then the frame is added to the alignments file with no faces, so that they video
        meta data can be stored.

        Parameters
        ----------
        pts_time: list
            A list of presentation timestamps (`float`) in frame index order for every frame in
            the input video
        keyframes: list
            A list of frame indices corresponding to the key frames in the input video
        """
        if pts_time[0] != 0:
            pts_time, keyframes = self._pad_leading_frames(pts_time, keyframes)

        sample_filename = next(fname for fname in self.data)
        basename = sample_filename[:sample_filename.rfind("_")]
        logger.debug("sample filename: %s, base filename: %s", sample_filename, basename)
        logger.info("Saving video meta information to Alignments file")

        for idx, pts in enumerate(pts_time):
            meta = dict(pts_time=pts, keyframe=idx in keyframes)
            key = f"{basename}_{idx + 1:06d}.png"
            if key not in self.data:
                self.data[key] = dict(video_meta=meta, faces=[])
            else:
                self.data[key]["video_meta"] = meta

        logger.debug("Alignments count: %s, timestamp count: %s", len(self.data), len(pts_time))
        if len(self.data) != len(pts_time):
            raise FaceswapError(
                "There is a mismatch between the number of frames found in the video file "
                f"({len(pts_time)}) and the number of frames found in the alignments file "
                f"({len(self.data)}).\nThis can be caused by a number of issues:"
                "\n  - The video has a Variable Frame Rate and FFMPEG is having a hard time "
                "calculating the correct number of frames."
                "\n  - You are working with a Merged Alignments file. This is not supported for "
                "your current use case."
                "\nYou should either extract the video to individual frames, re-encode the "
                "video at a constant frame rate and re-run extraction or work with a dedicated "
                "alignments file for your requested video.")
        self.save()

    @classmethod
    def _pad_leading_frames(cls, pts_time, keyframes):
        """ Calculate the number of frames to pad the video by when the first frame is not
        a key frame.

        A somewhat crude method by obtaining the gaps between existing frames and calculating
        how many frames should be inserted at the beginning based on the first presentation
        timestamp.

        Parameters
        ----------
         pts_time: list
            A list of presentation timestamps (`float`) in frame index order for every frame in
            the input video

        Returns
        -------
        tuple
            The presentation time stamps with extra frames padded to the beginning and the
            keyframes adjusted to include the new frames
        """
        start_pts = pts_time[0]
        logger.debug("Video not cut on keyframe. Start pts: %s", start_pts)
        gaps = []
        prev_time = None
        for item in pts_time:
            if prev_time is not None:
                gaps.append(item - prev_time)
            prev_time = item
        data_points = len(gaps)
        avg_gap = sum(gaps) / data_points
        frame_count = int(round(start_pts / avg_gap))
        pad_pts = [avg_gap * i for i in range(frame_count)]
        logger.debug("data_points: %s, avg_gap: %s, frame_count: %s, pad_pts: %s",
                     data_points, avg_gap, frame_count, pad_pts)
        pts_time = pad_pts + pts_time
        keyframes = [i + frame_count for i in keyframes]
        return pts_time, keyframes

    # << VALIDATION >> #

    def frame_exists(self, frame_name):
        """ Check whether a given frame_name exists within the alignments :attr:`data`.

        Parameters
        ----------
        frame_name: str
            The frame name to check. This should be the base name of the frame, not the full path

        Returns
        -------
        bool
            ``True`` if the given frame_name exists within the alignments :attr:`data`
            otherwise ``False``
        """
        retval = frame_name in self._data.keys()
        logger.trace("'%s': %s", frame_name, retval)
        return retval

    def frame_has_faces(self, frame_name):
        """ Check whether a given frame_name exists within the alignments :attr:`data` and contains
        at least 1 face.

        Parameters
        ----------
        frame_name: str
            The frame name to check. This should be the base name of the frame, not the full path

        Returns
        -------
        bool
            ``True`` if the given frame_name exists within the alignments :attr:`data` and has at
            least 1 face associated with it, otherwise ``False``
        """
        retval = bool(self._data.get(frame_name, {}).get("faces", []))
        logger.trace("'%s': %s", frame_name, retval)
        return retval

    def frame_has_multiple_faces(self, frame_name):
        """ Check whether a given frame_name exists within the alignments :attr:`data` and contains
        more than 1 face.

        Parameters
        ----------
        frame_name: str
            The frame_name name to check. This should be the base name of the frame, not the full
            path

        Returns
        -------
        bool
            ``True`` if the given frame_name exists within the alignments :attr:`data` and has more
            than 1 face associated with it, otherwise ``False``
        """
        if not frame_name:
            retval = False
        else:
            retval = bool(len(self._data.get(frame_name, {}).get("faces", [])) > 1)
        logger.trace("'%s': %s", frame_name, retval)
        return retval

    def mask_is_valid(self, mask_type):
        """ Ensure the given ``mask_type`` is valid for the alignments :attr:`data`.

        Every face in the alignments :attr:`data` must have the given mask type to successfully
        pass the test.

        Parameters
        ----------
        mask_type: str
            The mask type to check against the current alignments :attr:`data`

        Returns
        -------
        bool:
            ``True`` if all faces in the current alignments possess the given ``mask_type``
            otherwise ``False``
        """
        retval = any([(face.get("mask", None) is not None and
                       face["mask"].get(mask_type, None) is not None)
                      for val in self._data.values()
                      for face in val["faces"]])
        logger.debug(retval)
        return retval

    # << DATA >> #

    def get_faces_in_frame(self, frame_name):
        """ Obtain the faces from :attr:`data` associated with a given frame_name.

        Parameters
        ----------
        frame_name: str
            The frame name to return faces for. This should be the base name of the frame, not the
            full path

        Returns
        -------
        list
            The list of face dictionaries that appear within the requested frame_name
        """
        logger.trace("Getting faces for frame_name: '%s'", frame_name)
        return self._data.get(frame_name, {}).get("faces", [])

    def _count_faces_in_frame(self, frame_name):
        """ Return number of faces that appear within :attr:`data` for the given frame_name.

        Parameters
        ----------
        frame_name: str
            The frame name to return the count for. This should be the base name of the frame, not
            the full path

        Returns
        -------
        int
            The number of faces that appear in the given frame_name
        """
        retval = len(self._data.get(frame_name, {}).get("faces", []))
        logger.trace(retval)
        return retval

    # << MANIPULATION >> #

    def delete_face_at_index(self, frame_name, face_index):
        """ Delete the face for the given frame_name at the given face index from :attr:`data`.

        Parameters
        ----------
        frame_name: str
            The frame name to remove the face from. This should be the base name of the frame, not
            the full path
        face_index: int
            The index number of the face within the given frame_name to remove

        Returns
        -------
        bool
            ``True`` if a face was successfully deleted otherwise ``False``
        """
        logger.debug("Deleting face %s for frame_name '%s'", face_index, frame_name)
        face_index = int(face_index)
        if face_index + 1 > self._count_faces_in_frame(frame_name):
            logger.debug("No face to delete: (frame_name: '%s', face_index %s)",
                         frame_name, face_index)
            return False
        del self._data[frame_name]["faces"][face_index]
        logger.debug("Deleted face: (frame_name: '%s', face_index %s)", frame_name, face_index)
        return True

    def add_face(self, frame_name, face):
        """ Add a new face for the given frame_name in :attr:`data` and return it's index.

        Parameters
        ----------
        frame_name: str
            The frame name to add the face to. This should be the base name of the frame, not the
            full path
        face: dict
            The face information to add to the given frame_name, correctly formatted for storing in
            :attr:`data`

        Returns
        -------
        int
            The index of the newly added face within :attr:`data` for the given frame_name
        """
        logger.debug("Adding face to frame_name: '%s'", frame_name)
        if frame_name not in self._data:
            self._data[frame_name] = dict(faces=[])
        self._data[frame_name]["faces"].append(face)
        retval = self._count_faces_in_frame(frame_name) - 1
        logger.debug("Returning new face index: %s", retval)
        return retval

    def update_face(self, frame_name, face_index, face):
        """ Update the face for the given frame_name at the given face index in :attr:`data`.

        Parameters
        ----------
        frame_name: str
            The frame name to update the face for. This should be the base name of the frame, not
            the full path
        face_index: int
            The index number of the face within the given frame_name to update
        face: dict
            The face information to update to the given frame_name at the given face_index,
            correctly formatted for storing in :attr:`data`
        """
        logger.debug("Updating face %s for frame_name '%s'", face_index, frame_name)
        self._data[frame_name]["faces"][face_index] = face

    def filter_faces(self, filter_dict, filter_out=False):
        """ Remove faces from :attr:`data` based on a given filter list.

        Parameters
        ----------
        filter_dict: dict
            Dictionary of source filenames as key with a list of face indices to filter as value.
        filter_out: bool, optional
            ``True`` if faces should be removed from :attr:`data` when there is a corresponding
            match in the given filter_dict. ``False`` if faces should be kept in :attr:`data` when
            there is a corresponding match in the given filter_dict, but removed if there is no
            match. Default: ``False``
        """
        logger.debug("filter_dict: %s, filter_out: %s", filter_dict, filter_out)
        for source_frame, frame_data in self._data.items():
            face_indices = filter_dict.get(source_frame, [])
            if filter_out:
                filter_list = face_indices
            else:
                filter_list = [idx for idx in range(len(frame_data["faces"]))
                               if idx not in face_indices]
            logger.trace("frame: '%s', filter_list: %s", source_frame, filter_list)

            for face_idx in reversed(sorted(filter_list)):
                logger.verbose("Filtering out face: (filename: %s, index: %s)",
                               source_frame, face_idx)
                del frame_data["faces"][face_idx]

    # << GENERATORS >> #
    def yield_faces(self):
        """ Generator to obtain all faces with meta information from :attr:`data`. The results
        are yielded by frame.

        Notes
        -----
        The yielded order is non-deterministic.

        Yields
        ------
        frame_name: str
            The frame name that the face belongs to. This is the base name of the frame, as it
            appears in :attr:`data`, not the full path
        faces: list
            The list of face `dict` objects that exist for this frame
        face_count: int
            The number of faces that exist within :attr:`data` for this frame
        frame_fullname: str
            The full path (folder and filename) for the yielded frame
        """
        for frame_fullname, val in self._data.items():
            frame_name = os.path.splitext(frame_fullname)[0]
            face_count = len(val["faces"])
            logger.trace("Yielding: (frame: '%s', faces: %s, frame_fullname: '%s')",
                         frame_name, face_count, frame_fullname)
            yield frame_name, val["faces"], face_count, frame_fullname

    # << LEGACY FUNCTIONS >> #

    def _update_legacy(self):
        """ Check whether the alignments are legacy, and if so update them to current alignments
        format. """
        updated = False
        if self._has_legacy_structure():
            self._update_legacy_structure()

        if self._has_legacy_landmarksxy():
            logger.info("Updating legacy landmarksXY to landmarks_xy")
            self._update_legacy_landmarksxy()
            updated = True
        if self._has_legacy_landmarks_list():
            logger.info("Updating legacy landmarks from list to numpy array")
            self._update_legacy_landmarks_list()
            updated = True
        if self._version < 2.2:
            logger.info("Updating legacy mask centering")
            self._update_mask_centering()
            updated = True
        if updated:
            self._version = _VERSION
            self.save()

    # <File Format> #
    # Serializer is now a compressed pickle custom format. This used to be any number
    # of serializers
    def _test_for_legacy(self, location):
        """ For alignments filenames passed in without an extension, test for legacy
        serialization formats and update to current ``.fsa`` format if any are found.

        Parameters
        ----------
        location: str
            The folder location to check for legacy alignments
        """
        logger.debug("Checking for legacy alignments file formats: '%s'", location)
        filename = os.path.splitext(location)[0]
        for ext in (".json", ".p", ".pickle", ".yaml"):
            legacy_filename = f"{filename}{ext}"
            if os.path.exists(legacy_filename):
                logger.debug("Legacy alignments file exists: '%s'", legacy_filename)
                _ = self._update_file_format(*os.path.split(legacy_filename))
                break
            logger.debug("Legacy alignments file does not exist: '%s'", legacy_filename)

    def _update_file_format(self, folder, filename):
        """ Convert old style serialized alignments to new ``.fsa`` format.

        Parameters
        ----------
        folder: str
            The folder that the legacy alignments exist in
        filename: str
            The file name of the legacy alignments

        Returns
        -------
        str
            The full path to the newly created ``.fsa`` alignments file
        """
        logger.info("Reformatting legacy alignments file...")
        old_location = os.path.join(str(folder), filename)
        new_location = f"{os.path.splitext(old_location)[0]}.{self._serializer.file_extension}"
        if os.path.exists(old_location):
            if os.path.exists(new_location):
                logger.info("Using existing updated alignments file found at '%s'. If you do not "
                            "wish to use this existing file then you should delete or rename it.",
                            new_location)
            else:
                logger.info("Old location: '%s', New location: '%s'", old_location, new_location)
                load_serializer = get_serializer_from_filename(old_location)
                data = load_serializer.load(old_location)
                self._serializer.save(new_location, data)
        return os.path.basename(new_location)

    # <Structure> #
    # Alignments were structured: {frame_name: <list of faces>}. We need to be able to store
    # information at the frame level, so new structure is:  {frame_name: {faces: <list of faces>}}
    def _has_legacy_structure(self):
        """ Test whether the alignments file is laid out in the old structure of
        `{frame_name: [faces]}`

        Returns
        -------
        bool
            ``True`` if the file has legacy structure otherwise ``False``
        """
        retval = any(isinstance(val, list) for val in self._data.values())
        logger.debug("legacy structure: %s", retval)
        return retval

    def _update_legacy_structure(self):
        """ Update legacy alignments files from the format `{frame_name: [faces}` to the
        format `{frame_name: {faces: [faces]}`."""
        for key, val in self._data.items():
            self._data[key] = dict(faces=val)
        logger.debug("Updated alignments file structure")

    # <landmarks> #
    # Landmarks renamed from landmarksXY to landmarks_xy for PEP compliance
    def _has_legacy_landmarksxy(self):
        """ check for legacy landmarksXY keys.

        Returns
        -------
        bool
            ``True`` if the alignments file contains legacy `landmarksXY` keys otherwise ``False``
        """
        logger.debug("checking legacy landmarksXY")
        retval = (any(key == "landmarksXY"
                      for val in self._data.values()
                      for alignment in val["faces"]
                      for key in alignment))
        logger.debug("legacy landmarksXY: %s", retval)
        return retval

    def _update_legacy_landmarksxy(self):
        """ Update legacy `landmarksXY` keys to PEP compliant `landmarks_xy` keys. """
        update_count = 0
        for val in self._data.values():
            for alignment in val["faces"]:
                alignment["landmarks_xy"] = alignment.pop("landmarksXY")
                update_count += 1
        logger.debug("Updated landmarks_xy: %s", update_count)

    # Landmarks stored as list instead of numpy array
    def _has_legacy_landmarks_list(self):
        """ check for legacy landmarks stored as `list` rather than :class:`numpy.ndarray`.

        Returns
        -------
        bool
            ``True`` if not all landmarks are :class:`numpy.ndarray` otherwise ``False``
        """
        logger.debug("checking legacy landmarks as list")
        retval = not all(isinstance(face["landmarks_xy"], np.ndarray)
                         for val in self._data.values()
                         for face in val["faces"])
        return retval

    def _update_legacy_landmarks_list(self):
        """ Update landmarks stored as `list` to :class:`numpy.ndarray`. """
        update_count = 0
        for val in self._data.values():
            for alignment in val["faces"]:
                test = alignment["landmarks_xy"]
                if not isinstance(test, np.ndarray):
                    alignment["landmarks_xy"] = np.array(test, dtype="float32")
                    update_count += 1
        logger.debug("Updated landmarks_xy: %s", update_count)

    # Masks not containing the stored_centering parameters. Prior to this implementation all masks
    # were stored with face centering
    def _update_mask_centering(self):
        update_count = 0
        for val in self._data.values():
            for alignment in val["faces"]:
                if "mask" not in alignment:
                    alignment["mask"] = {}
                for mask in alignment["mask"].values():
                    mask["stored_centering"] = "face"
                    update_count += 1
        logger.debug("Updated legacy mask centering: %s", update_count)


class Thumbnails():
    """ Thumbnail images stored in the alignments file.

    The thumbnails are stored as low resolution (64px), low quality jpg in the alignments file
    and are used for the Manual Alignments tool.

    Parameters
    ----------
    alignments: :class:'~lib.align.Alignments`
        The parent alignments class that these thumbs belong to
    """
    def __init__(self, alignments):
        logger.debug("Initializing %s: (alignments: %s)", self.__class__.__name__, alignments)
        self._alignments_dict = alignments.data
        self._frame_list = list(sorted(self._alignments_dict))
        logger.debug("Initialized %s", self.__class__.__name__)

    @property
    def has_thumbnails(self):
        """ bool: ``True`` if all faces in the alignments file contain thumbnail images
        otherwise ``False``. """
        retval = all(np.any(face.get("thumb"))
                     for frame in self._alignments_dict.values()
                     for face in frame["faces"])
        logger.trace(retval)
        return retval

    def get_thumbnail_by_index(self, frame_index, face_index):
        """ Obtain a jpg thumbnail from the given frame index for the given face index

        Parameters
        ----------
        frame_index: int
            The frame index that contains the thumbnail
        face_index: int
            The face index within the frame to retrieve the thumbnail for

        Returns
        -------
        :class:`numpy.ndarray`
            The encoded jpg thumbnail
        """
        retval = self._alignments_dict[self._frame_list[frame_index]]["faces"][face_index]["thumb"]
        logger.trace("frame index: %s, face_index: %s, thumb shape: %s",
                     frame_index, face_index, retval.shape)
        return retval

    def add_thumbnail(self, frame, face_index, thumb):
        """ Add a thumbnail for the given face index for the given frame.

        Parameters
        ----------
        frame: str
            The name of the frame to add the thumbnail for
        face_index: int
            The face index within the given frame to add the thumbnail for
        thumb: :class:`numpy.ndarray`
            The encoded jpg thumbnail at 64px to add to the alignments file
        """
        logger.debug("frame: %s, face_index: %s, thumb shape: %s thumb dtype: %s",
                     frame, face_index, thumb.shape, thumb.dtype)
        self._alignments_dict[frame]["faces"][face_index]["thumb"] = thumb
