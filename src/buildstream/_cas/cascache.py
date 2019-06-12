#
#  Copyright (C) 2018 Codethink Limited
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.	 See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library. If not, see <http://www.gnu.org/licenses/>.
#
#  Authors:
#        Jürg Billeter <juerg.billeter@codethink.co.uk>

import hashlib
import itertools
import os
import stat
import errno
import uuid
import contextlib

import grpc

from .._protos.build.bazel.remote.execution.v2 import remote_execution_pb2
from .._protos.buildstream.v2 import buildstream_pb2

from .. import utils
from .._exceptions import CASCacheError

from .casremote import BlobNotFound, _CASBatchRead, _CASBatchUpdate

_BUFFER_SIZE = 65536


CACHE_SIZE_FILE = "cache_size"


# A CASCache manages a CAS repository as specified in the Remote Execution API.
#
# Args:
#     path (str): The root directory for the CAS repository
#     cache_quota (int): User configured cache quota
#
class CASCache():

    def __init__(self, path):
        self.casdir = os.path.join(path, 'cas')
        self.tmpdir = os.path.join(path, 'tmp')
        os.makedirs(os.path.join(self.casdir, 'refs', 'heads'), exist_ok=True)
        os.makedirs(os.path.join(self.casdir, 'objects'), exist_ok=True)
        os.makedirs(self.tmpdir, exist_ok=True)

        self.__reachable_directory_callbacks = []
        self.__reachable_digest_callbacks = []

    # preflight():
    #
    # Preflight check.
    #
    def preflight(self):
        headdir = os.path.join(self.casdir, 'refs', 'heads')
        objdir = os.path.join(self.casdir, 'objects')
        if not (os.path.isdir(headdir) and os.path.isdir(objdir)):
            raise CASCacheError("CAS repository check failed for '{}'".format(self.casdir))

    # contains():
    #
    # Check whether the specified ref is already available in the local CAS cache.
    #
    # Args:
    #     ref (str): The ref to check
    #
    # Returns: True if the ref is in the cache, False otherwise
    #
    def contains(self, ref):
        refpath = self._refpath(ref)

        # This assumes that the repository doesn't have any dangling pointers
        return os.path.exists(refpath)

    # contains_file():
    #
    # Check whether a digest corresponds to a file which exists in CAS
    #
    # Args:
    #     digest (Digest): The file digest to check
    #
    # Returns: True if the file is in the cache, False otherwise
    #
    def contains_file(self, digest):
        return os.path.exists(self.objpath(digest))

    # contains_directory():
    #
    # Check whether the specified directory and subdirectories are in the cache,
    # i.e non dangling.
    #
    # Args:
    #     digest (Digest): The directory digest to check
    #     with_files (bool): Whether to check files as well
    #     update_mtime (bool): Whether to update the timestamp
    #
    # Returns: True if the directory is available in the local cache
    #
    def contains_directory(self, digest, *, with_files, update_mtime=False):
        try:
            directory = remote_execution_pb2.Directory()
            path = self.objpath(digest)
            with open(path, 'rb') as f:
                directory.ParseFromString(f.read())
                if update_mtime:
                    os.utime(f.fileno())

            # Optionally check presence of files
            if with_files:
                for filenode in directory.files:
                    path = self.objpath(filenode.digest)
                    if update_mtime:
                        # No need for separate `exists()` call as this will raise
                        # FileNotFoundError if the file does not exist.
                        os.utime(path)
                    elif not os.path.exists(path):
                        return False

            # Check subdirectories
            for dirnode in directory.directories:
                if not self.contains_directory(dirnode.digest, with_files=with_files, update_mtime=update_mtime):
                    return False

            return True
        except FileNotFoundError:
            return False

    # checkout():
    #
    # Checkout the specified directory digest.
    #
    # Args:
    #     dest (str): The destination path
    #     tree (Digest): The directory digest to extract
    #     can_link (bool): Whether we can create hard links in the destination
    #
    def checkout(self, dest, tree, *, can_link=False):
        os.makedirs(dest, exist_ok=True)

        directory = remote_execution_pb2.Directory()

        with open(self.objpath(tree), 'rb') as f:
            directory.ParseFromString(f.read())

        for filenode in directory.files:
            # regular file, create hardlink
            fullpath = os.path.join(dest, filenode.name)
            if can_link:
                utils.safe_link(self.objpath(filenode.digest), fullpath)
            else:
                utils.safe_copy(self.objpath(filenode.digest), fullpath)

            if filenode.is_executable:
                os.chmod(fullpath, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR |
                         stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

        for dirnode in directory.directories:
            fullpath = os.path.join(dest, dirnode.name)
            self.checkout(fullpath, dirnode.digest, can_link=can_link)

        for symlinknode in directory.symlinks:
            # symlink
            fullpath = os.path.join(dest, symlinknode.name)
            os.symlink(symlinknode.target, fullpath)

    # diff():
    #
    # Return a list of files that have been added or modified between
    # the refs described by ref_a and ref_b.
    #
    # Args:
    #     ref_a (str): The first ref
    #     ref_b (str): The second ref
    #     subdir (str): A subdirectory to limit the comparison to
    #
    def diff(self, ref_a, ref_b):
        tree_a = self.resolve_ref(ref_a)
        tree_b = self.resolve_ref(ref_b)

        added = []
        removed = []
        modified = []

        self.diff_trees(tree_a, tree_b, added=added, removed=removed, modified=modified)

        return modified, removed, added

    # pull():
    #
    # Pull a ref from a remote repository.
    #
    # Args:
    #     ref (str): The ref to pull
    #     remote (CASRemote): The remote repository to pull from
    #
    # Returns:
    #   (bool): True if pull was successful, False if ref was not available
    #
    def pull(self, ref, remote):
        try:
            remote.init()

            request = buildstream_pb2.GetReferenceRequest(instance_name=remote.spec.instance_name)
            request.key = ref
            response = remote.ref_storage.GetReference(request)

            tree = response.digest

            # Fetch Directory objects
            self._fetch_directory(remote, tree)

            # Fetch files, excluded_subdirs determined in pullqueue
            required_blobs = self.required_blobs_for_directory(tree)
            missing_blobs = self.local_missing_blobs(required_blobs)
            if missing_blobs:
                self.fetch_blobs(remote, missing_blobs)

            self.set_ref(ref, tree)

            return True
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.NOT_FOUND:
                raise CASCacheError("Failed to pull ref {}: {}".format(ref, e)) from e
            else:
                return False
        except BlobNotFound:
            return False

    # pull_tree():
    #
    # Pull a single Tree rather than a ref.
    # Does not update local refs.
    #
    # Args:
    #     remote (CASRemote): The remote to pull from
    #     digest (Digest): The digest of the tree
    #
    def pull_tree(self, remote, digest):
        try:
            remote.init()

            digest = self._fetch_tree(remote, digest)

            return digest

        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.NOT_FOUND:
                raise

        return None

    # push():
    #
    # Push committed refs to remote repository.
    #
    # Args:
    #     refs (list): The refs to push
    #     remote (CASRemote): The remote to push to
    #
    # Returns:
    #   (bool): True if any remote was updated, False if no pushes were required
    #
    # Raises:
    #   (CASCacheError): if there was an error
    #
    def push(self, refs, remote):
        skipped_remote = True
        try:
            for ref in refs:
                tree = self.resolve_ref(ref)

                # Check whether ref is already on the server in which case
                # there is no need to push the ref
                try:
                    request = buildstream_pb2.GetReferenceRequest(instance_name=remote.spec.instance_name)
                    request.key = ref
                    response = remote.ref_storage.GetReference(request)

                    if response.digest.hash == tree.hash and response.digest.size_bytes == tree.size_bytes:
                        # ref is already on the server with the same tree
                        continue

                except grpc.RpcError as e:
                    if e.code() != grpc.StatusCode.NOT_FOUND:
                        # Intentionally re-raise RpcError for outer except block.
                        raise

                self._send_directory(remote, tree)

                request = buildstream_pb2.UpdateReferenceRequest(instance_name=remote.spec.instance_name)
                request.keys.append(ref)
                request.digest.CopyFrom(tree)
                remote.ref_storage.UpdateReference(request)

                skipped_remote = False
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.RESOURCE_EXHAUSTED:
                raise CASCacheError("Failed to push ref {}: {}".format(refs, e), temporary=True) from e

        return not skipped_remote

    # objpath():
    #
    # Return the path of an object based on its digest.
    #
    # Args:
    #     digest (Digest): The digest of the object
    #
    # Returns:
    #     (str): The path of the object
    #
    def objpath(self, digest):
        return os.path.join(self.casdir, 'objects', digest.hash[:2], digest.hash[2:])

    # add_object():
    #
    # Hash and write object to CAS.
    #
    # Args:
    #     digest (Digest): An optional Digest object to populate
    #     path (str): Path to file to add
    #     buffer (bytes): Byte buffer to add
    #     link_directly (bool): Whether file given by path can be linked
    #
    # Returns:
    #     (Digest): The digest of the added object
    #
    # Either `path` or `buffer` must be passed, but not both.
    #
    def add_object(self, *, digest=None, path=None, buffer=None, link_directly=False):
        # Exactly one of the two parameters has to be specified
        assert (path is None) != (buffer is None)

        # If we're linking directly, then path must be specified.
        assert (not link_directly) or (link_directly and path)

        if digest is None:
            digest = remote_execution_pb2.Digest()

        try:
            h = hashlib.sha256()
            # Always write out new file to avoid corruption if input file is modified
            with contextlib.ExitStack() as stack:
                if path is not None and link_directly:
                    tmp = stack.enter_context(open(path, 'rb'))
                    for chunk in iter(lambda: tmp.read(_BUFFER_SIZE), b""):
                        h.update(chunk)
                else:
                    tmp = stack.enter_context(self._temporary_object())

                    if path:
                        with open(path, 'rb') as f:
                            for chunk in iter(lambda: f.read(_BUFFER_SIZE), b""):
                                h.update(chunk)
                                tmp.write(chunk)
                    else:
                        h.update(buffer)
                        tmp.write(buffer)

                    tmp.flush()

                digest.hash = h.hexdigest()
                digest.size_bytes = os.fstat(tmp.fileno()).st_size

                # Place file at final location
                objpath = self.objpath(digest)
                os.makedirs(os.path.dirname(objpath), exist_ok=True)
                os.link(tmp.name, objpath)

        except FileExistsError:
            # We can ignore the failed link() if the object is already in the repo.
            pass

        except OSError as e:
            raise CASCacheError("Failed to hash object: {}".format(e)) from e

        return digest

    # set_ref():
    #
    # Create or replace a ref.
    #
    # Args:
    #     ref (str): The name of the ref
    #
    def set_ref(self, ref, tree):
        refpath = self._refpath(ref)
        os.makedirs(os.path.dirname(refpath), exist_ok=True)
        with utils.save_file_atomic(refpath, 'wb', tempdir=self.tmpdir) as f:
            f.write(tree.SerializeToString())

    # resolve_ref():
    #
    # Resolve a ref to a digest.
    #
    # Args:
    #     ref (str): The name of the ref
    #     update_mtime (bool): Whether to update the mtime of the ref
    #
    # Returns:
    #     (Digest): The digest stored in the ref
    #
    def resolve_ref(self, ref, *, update_mtime=False):
        refpath = self._refpath(ref)

        try:
            with open(refpath, 'rb') as f:
                if update_mtime:
                    os.utime(refpath)

                digest = remote_execution_pb2.Digest()
                digest.ParseFromString(f.read())
                return digest

        except FileNotFoundError as e:
            raise CASCacheError("Attempt to access unavailable ref: {}".format(e)) from e

    # update_mtime()
    #
    # Update the mtime of a ref.
    #
    # Args:
    #     ref (str): The ref to update
    #
    def update_mtime(self, ref):
        try:
            os.utime(self._refpath(ref))
        except FileNotFoundError as e:
            raise CASCacheError("Attempt to access unavailable ref: {}".format(e)) from e

    # remove():
    #
    # Removes the given symbolic ref from the repo.
    #
    # Args:
    #    ref (str): A symbolic ref
    #    basedir (str): Path of base directory the ref is in, defaults to
    #                   CAS refs heads
    #
    def remove(self, ref, *, basedir=None):

        if basedir is None:
            basedir = os.path.join(self.casdir, 'refs', 'heads')
        # Remove cache ref
        self._remove_ref(ref, basedir)

    # adds callback of iterator over reachable directory digests
    def add_reachable_directories_callback(self, callback):
        self.__reachable_directory_callbacks.append(callback)

    # adds callbacks of iterator over reachable file digests
    def add_reachable_digests_callback(self, callback):
        self.__reachable_digest_callbacks.append(callback)

    def update_tree_mtime(self, tree):
        reachable = set()
        self._reachable_refs_dir(reachable, tree, update_mtime=True)

    # remote_missing_blobs_for_directory():
    #
    # Determine which blobs of a directory tree are missing on the remote.
    #
    # Args:
    #     digest (Digest): The directory digest
    #
    # Returns: List of missing Digest objects
    #
    def remote_missing_blobs_for_directory(self, remote, digest):
        required_blobs = self.required_blobs_for_directory(digest)

        return self.remote_missing_blobs(remote, required_blobs)

    # remote_missing_blobs():
    #
    # Determine which blobs are missing on the remote.
    #
    # Args:
    #     blobs ([Digest]): List of directory digests to check
    #
    # Returns: List of missing Digest objects
    #
    def remote_missing_blobs(self, remote, blobs):
        missing_blobs = dict()
        # Limit size of FindMissingBlobs request
        for required_blobs_group in _grouper(iter(blobs), 512):
            request = remote_execution_pb2.FindMissingBlobsRequest(instance_name=remote.spec.instance_name)

            for required_digest in required_blobs_group:
                d = request.blob_digests.add()
                d.CopyFrom(required_digest)

            response = remote.cas.FindMissingBlobs(request)
            for missing_digest in response.missing_blob_digests:
                d = remote_execution_pb2.Digest()
                d.CopyFrom(missing_digest)
                missing_blobs[d.hash] = d

        return missing_blobs.values()

    # local_missing_blobs():
    #
    # Check local cache for missing blobs.
    #
    # Args:
    #    digests (list): The Digests of blobs to check
    #
    # Returns: Missing Digest objects
    #
    def local_missing_blobs(self, digests):
        missing_blobs = []
        for digest in digests:
            objpath = self.objpath(digest)
            if not os.path.exists(objpath):
                missing_blobs.append(digest)
        return missing_blobs

    # required_blobs_for_directory():
    #
    # Generator that returns the Digests of all blobs in the tree specified by
    # the Digest of the toplevel Directory object.
    #
    def required_blobs_for_directory(self, directory_digest, *, excluded_subdirs=None):
        if not excluded_subdirs:
            excluded_subdirs = []

        # parse directory, and recursively add blobs

        yield directory_digest

        directory = remote_execution_pb2.Directory()

        with open(self.objpath(directory_digest), 'rb') as f:
            directory.ParseFromString(f.read())

        for filenode in directory.files:
            yield filenode.digest

        for dirnode in directory.directories:
            if dirnode.name not in excluded_subdirs:
                yield from self.required_blobs_for_directory(dirnode.digest)

    def diff_trees(self, tree_a, tree_b, *, added, removed, modified, path=""):
        dir_a = remote_execution_pb2.Directory()
        dir_b = remote_execution_pb2.Directory()

        if tree_a:
            with open(self.objpath(tree_a), 'rb') as f:
                dir_a.ParseFromString(f.read())
        if tree_b:
            with open(self.objpath(tree_b), 'rb') as f:
                dir_b.ParseFromString(f.read())

        a = 0
        b = 0
        while a < len(dir_a.files) or b < len(dir_b.files):
            if b < len(dir_b.files) and (a >= len(dir_a.files) or
                                         dir_a.files[a].name > dir_b.files[b].name):
                added.append(os.path.join(path, dir_b.files[b].name))
                b += 1
            elif a < len(dir_a.files) and (b >= len(dir_b.files) or
                                           dir_b.files[b].name > dir_a.files[a].name):
                removed.append(os.path.join(path, dir_a.files[a].name))
                a += 1
            else:
                # File exists in both directories
                if dir_a.files[a].digest.hash != dir_b.files[b].digest.hash:
                    modified.append(os.path.join(path, dir_a.files[a].name))
                a += 1
                b += 1

        a = 0
        b = 0
        while a < len(dir_a.directories) or b < len(dir_b.directories):
            if b < len(dir_b.directories) and (a >= len(dir_a.directories) or
                                               dir_a.directories[a].name > dir_b.directories[b].name):
                self.diff_trees(None, dir_b.directories[b].digest,
                                added=added, removed=removed, modified=modified,
                                path=os.path.join(path, dir_b.directories[b].name))
                b += 1
            elif a < len(dir_a.directories) and (b >= len(dir_b.directories) or
                                                 dir_b.directories[b].name > dir_a.directories[a].name):
                self.diff_trees(dir_a.directories[a].digest, None,
                                added=added, removed=removed, modified=modified,
                                path=os.path.join(path, dir_a.directories[a].name))
                a += 1
            else:
                # Subdirectory exists in both directories
                if dir_a.directories[a].digest.hash != dir_b.directories[b].digest.hash:
                    self.diff_trees(dir_a.directories[a].digest, dir_b.directories[b].digest,
                                    added=added, removed=removed, modified=modified,
                                    path=os.path.join(path, dir_a.directories[a].name))
                a += 1
                b += 1

    ################################################
    #             Local Private Methods            #
    ################################################

    def _refpath(self, ref):
        return os.path.join(self.casdir, 'refs', 'heads', ref)

    # _remove_ref()
    #
    # Removes a ref.
    #
    # This also takes care of pruning away directories which can
    # be removed after having removed the given ref.
    #
    # Args:
    #    ref (str): The ref to remove
    #    basedir (str): Path of base directory the ref is in
    #
    # Raises:
    #    (CASCacheError): If the ref didnt exist, or a system error
    #                     occurred while removing it
    #
    def _remove_ref(self, ref, basedir):

        # Remove the ref itself
        refpath = os.path.join(basedir, ref)

        try:
            os.unlink(refpath)
        except FileNotFoundError as e:
            raise CASCacheError("Could not find ref '{}'".format(ref)) from e

        # Now remove any leading directories

        components = list(os.path.split(ref))
        while components:
            components.pop()
            refdir = os.path.join(basedir, *components)

            # Break out once we reach the base
            if refdir == basedir:
                break

            try:
                os.rmdir(refdir)
            except FileNotFoundError:
                # The parent directory did not exist, but it's
                # parent directory might still be ready to prune
                pass
            except OSError as e:
                if e.errno == errno.ENOTEMPTY:
                    # The parent directory was not empty, so we
                    # cannot prune directories beyond this point
                    break

                # Something went wrong here
                raise CASCacheError("System error while removing ref '{}': {}".format(ref, e)) from e

    def _get_subdir(self, tree, subdir):
        head, name = os.path.split(subdir)
        if head:
            tree = self._get_subdir(tree, head)

        directory = remote_execution_pb2.Directory()

        with open(self.objpath(tree), 'rb') as f:
            directory.ParseFromString(f.read())

        for dirnode in directory.directories:
            if dirnode.name == name:
                return dirnode.digest

        raise CASCacheError("Subdirectory {} not found".format(name))

    def _reachable_refs_dir(self, reachable, tree, update_mtime=False, check_exists=False):
        if tree.hash in reachable:
            return
        try:
            if update_mtime:
                os.utime(self.objpath(tree))

            reachable.add(tree.hash)

            directory = remote_execution_pb2.Directory()

            with open(self.objpath(tree), 'rb') as f:
                directory.ParseFromString(f.read())

        except FileNotFoundError:
            if check_exists:
                raise

            # Just exit early if the file doesn't exist
            return

        for filenode in directory.files:
            if update_mtime:
                os.utime(self.objpath(filenode.digest))
            if check_exists:
                if not os.path.exists(self.objpath(filenode.digest)):
                    raise FileNotFoundError
            reachable.add(filenode.digest.hash)

        for dirnode in directory.directories:
            self._reachable_refs_dir(reachable, dirnode.digest, update_mtime=update_mtime, check_exists=check_exists)

    # _temporary_object():
    #
    # Returns:
    #     (file): A file object to a named temporary file.
    #
    # Create a named temporary file with 0o0644 access rights.
    @contextlib.contextmanager
    def _temporary_object(self):
        with utils._tempnamedfile(dir=self.tmpdir) as f:
            os.chmod(f.name,
                     stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            yield f

    # _ensure_blob():
    #
    # Fetch and add blob if it's not already local.
    #
    # Args:
    #     remote (Remote): The remote to use.
    #     digest (Digest): Digest object for the blob to fetch.
    #
    # Returns:
    #     (str): The path of the object
    #
    def _ensure_blob(self, remote, digest):
        objpath = self.objpath(digest)
        if os.path.exists(objpath):
            # already in local repository
            return objpath

        with self._temporary_object() as f:
            remote._fetch_blob(digest, f)

            added_digest = self.add_object(path=f.name, link_directly=True)
            assert added_digest.hash == digest.hash

        return objpath

    def _batch_download_complete(self, batch, *, missing_blobs=None):
        for digest, data in batch.send(missing_blobs=missing_blobs):
            with self._temporary_object() as f:
                f.write(data)
                f.flush()

                added_digest = self.add_object(path=f.name, link_directly=True)
                assert added_digest.hash == digest.hash

    # Helper function for _fetch_directory().
    def _fetch_directory_batch(self, remote, batch, fetch_queue, fetch_next_queue):
        self._batch_download_complete(batch)

        # All previously scheduled directories are now locally available,
        # move them to the processing queue.
        fetch_queue.extend(fetch_next_queue)
        fetch_next_queue.clear()
        return _CASBatchRead(remote)

    # Helper function for _fetch_directory().
    def _fetch_directory_node(self, remote, digest, batch, fetch_queue, fetch_next_queue, *, recursive=False):
        in_local_cache = os.path.exists(self.objpath(digest))

        if in_local_cache:
            # Skip download, already in local cache.
            pass
        elif (digest.size_bytes >= remote.max_batch_total_size_bytes or
              not remote.batch_read_supported):
            # Too large for batch request, download in independent request.
            self._ensure_blob(remote, digest)
            in_local_cache = True
        else:
            if not batch.add(digest):
                # Not enough space left in batch request.
                # Complete pending batch first.
                batch = self._fetch_directory_batch(remote, batch, fetch_queue, fetch_next_queue)
                batch.add(digest)

        if recursive:
            if in_local_cache:
                # Add directory to processing queue.
                fetch_queue.append(digest)
            else:
                # Directory will be available after completing pending batch.
                # Add directory to deferred processing queue.
                fetch_next_queue.append(digest)

        return batch

    # _fetch_directory():
    #
    # Fetches remote directory and adds it to content addressable store.
    #
    # This recursively fetches directory objects but doesn't fetch any
    # files.
    #
    # Args:
    #     remote (Remote): The remote to use.
    #     dir_digest (Digest): Digest object for the directory to fetch.
    #
    def _fetch_directory(self, remote, dir_digest):
        # TODO Use GetTree() if the server supports it

        fetch_queue = [dir_digest]
        fetch_next_queue = []
        batch = _CASBatchRead(remote)

        while len(fetch_queue) + len(fetch_next_queue) > 0:
            if not fetch_queue:
                batch = self._fetch_directory_batch(remote, batch, fetch_queue, fetch_next_queue)

            dir_digest = fetch_queue.pop(0)

            objpath = self._ensure_blob(remote, dir_digest)

            directory = remote_execution_pb2.Directory()
            with open(objpath, 'rb') as f:
                directory.ParseFromString(f.read())

            for dirnode in directory.directories:
                batch = self._fetch_directory_node(remote, dirnode.digest, batch,
                                                   fetch_queue, fetch_next_queue, recursive=True)

        # Fetch final batch
        self._fetch_directory_batch(remote, batch, fetch_queue, fetch_next_queue)

    def _fetch_tree(self, remote, digest):
        # download but do not store the Tree object
        with utils._tempnamedfile(dir=self.tmpdir) as out:
            remote._fetch_blob(digest, out)

            tree = remote_execution_pb2.Tree()

            with open(out.name, 'rb') as f:
                tree.ParseFromString(f.read())

            tree.children.extend([tree.root])
            for directory in tree.children:
                dirbuffer = directory.SerializeToString()
                dirdigest = self.add_object(buffer=dirbuffer)
                assert dirdigest.size_bytes == len(dirbuffer)

        return dirdigest

    # fetch_blobs():
    #
    # Fetch blobs from remote CAS. Returns missing blobs that could not be fetched.
    #
    # Args:
    #    remote (CASRemote): The remote repository to fetch from
    #    digests (list): The Digests of blobs to fetch
    #
    # Returns: The Digests of the blobs that were not available on the remote CAS
    #
    def fetch_blobs(self, remote, digests):
        missing_blobs = []

        remote.init()

        batch = _CASBatchRead(remote)

        for digest in digests:
            if (digest.size_bytes >= remote.max_batch_total_size_bytes or
                    not remote.batch_read_supported):
                # Too large for batch request, download in independent request.
                try:
                    self._ensure_blob(remote, digest)
                except grpc.RpcError as e:
                    if e.code() == grpc.StatusCode.NOT_FOUND:
                        missing_blobs.append(digest)
                    else:
                        raise CASCacheError("Failed to fetch blob: {}".format(e)) from e
            else:
                if not batch.add(digest):
                    # Not enough space left in batch request.
                    # Complete pending batch first.
                    self._batch_download_complete(batch, missing_blobs=missing_blobs)

                    batch = _CASBatchRead(remote)
                    batch.add(digest)

        # Complete last pending batch
        self._batch_download_complete(batch, missing_blobs=missing_blobs)

        return missing_blobs

    # send_blobs():
    #
    # Upload blobs to remote CAS.
    #
    # Args:
    #    remote (CASRemote): The remote repository to upload to
    #    digests (list): The Digests of Blobs to upload
    #
    def send_blobs(self, remote, digests, u_uid=uuid.uuid4()):
        batch = _CASBatchUpdate(remote)

        for digest in digests:
            with open(self.objpath(digest), 'rb') as f:
                assert os.fstat(f.fileno()).st_size == digest.size_bytes

                if (digest.size_bytes >= remote.max_batch_total_size_bytes or
                        not remote.batch_update_supported):
                    # Too large for batch request, upload in independent request.
                    remote._send_blob(digest, f, u_uid=u_uid)
                else:
                    if not batch.add(digest, f):
                        # Not enough space left in batch request.
                        # Complete pending batch first.
                        batch.send()
                        batch = _CASBatchUpdate(remote)
                        batch.add(digest, f)

        # Send final batch
        batch.send()

    def _send_directory(self, remote, digest, u_uid=uuid.uuid4()):
        missing_blobs = self.remote_missing_blobs_for_directory(remote, digest)

        # Upload any blobs missing on the server
        self.send_blobs(remote, missing_blobs, u_uid)


def _grouper(iterable, n):
    while True:
        try:
            current = next(iterable)
        except StopIteration:
            return
        yield itertools.chain([current], itertools.islice(iterable, n - 1))
