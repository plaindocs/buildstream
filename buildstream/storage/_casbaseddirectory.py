#!/usr/bin/env python3
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
#        Jim MacArthur <jim.macarthur@codethink.co.uk>

"""
CasBasedDirectory
=========

Implementation of the Directory class which backs onto a Merkle-tree based content
addressable storage system.

See also: :ref:`sandboxing`.
"""

from typing import List
from collections import OrderedDict

import os
import tempfile
import stat

from .._protos.build.bazel.remote.execution.v2 import remote_execution_pb2
from .._exceptions import BstError
from .directory import Directory, VirtualDirectoryError
from ._filebaseddirectory import FileBasedDirectory
from ..utils import FileListResult, safe_copy, list_relative_paths
from .._artifactcache.cascache import CASCache


class IndexEntry():
    """ Used in our index of names to objects to store the 'modified' flag
    for directory entries. Because we need both the remote_execution_pb2 object
    and our own Directory object for directory entries, we store both. For files
    and symlinks, only pb2_object is used. """
    def __init__(self, pb2_object, buildstream_object=None, modified=False):
        self.pb2_object = pb2_object
        self.buildstream_object = buildstream_object
        self.modified = modified


# CasBasedDirectory intentionally doesn't call its superclass constuctor,
# which is meant to be unimplemented.
# pylint: disable=super-init-not-called

class CasBasedDirectory(Directory):
    """
    CAS-based directories can have two names; one is a 'common name' which has no effect
    on functionality, and the 'filename'. If a CasBasedDirectory has a parent, then 'filename'
    must be the name of an entry in the parent directory's index which points to this object.
    This is used to inform a parent directory that it must update the given hash for this
    object when this object changes.

    Typically a top-level CasBasedDirectory will have a common_name and no filename, and
    subdirectories wil have a filename and no common_name. common_name can used to identify
    CasBasedDirectory objects in a log file, since they have no unique position in a file
    system.
    """
    def __init__(self, context, ref=None, parent=None, common_name="untitled", filename=None):
        self.context = context
        self.cas_directory = os.path.join(context.artifactdir, 'cas')
        self.filename = filename
        self.common_name = common_name
        self.pb2_directory = remote_execution_pb2.Directory()
        self.cas_cache = CASCache(context)
        if ref:
            with open(self.cas_cache.objpath(ref), 'rb') as f:
                self.pb2_directory.ParseFromString(f.read())

        self.ref = ref
        self.index = OrderedDict()
        self.parent = parent
        self._directory_read = False
        self._populate_index()

    def _populate_index(self) -> None:
        if self._directory_read:
            raise VirtualDirectoryError("_populate_index called twice")
        for entry in self.pb2_directory.directories:
            buildStreamDirectory = CasBasedDirectory(self.context, ref=entry.digest,
                                                     parent=self, filename=entry.name)
            self.index[entry.name] = IndexEntry(entry, buildstream_object=buildStreamDirectory)
        for entry in self.pb2_directory.files:
            self.index[entry.name] = IndexEntry(entry)
        for entry in self.pb2_directory.symlinks:
            self.index[entry.name] = IndexEntry(entry)
        self._directory_read = True

    def _recalculate_recursing_up(self, caller=None) -> None:
        """Recalcuate the hash for this directory and store the results in
        the cache.  If this directory has a parent, tell it to
        recalculate (since changing this directory changes an entry in
        the parent).

        """
        self.ref = self.cas_cache.add_object(buffer=self.pb2_directory.SerializeToString())
        if caller:
            old_dir = self.find_pb2_entry(caller.filename)
            self.cas_cache.add_object(digest=old_dir.digest, buffer=caller.pb2_directory.SerializeToString())
        if self.parent:
            self.parent._recalculate_recursing_up(self)

    def _recalculate_recursing_down(self, parent=None) -> None:
        """Recalcuate the hash for this directory and any
        subdirectories. Hashes for subdirectories should be calculated
        and stored after a significant operation (e.g. an
        import_files() call) but not after adding each file, as that
        is extremely wasteful.

        """
        for entry in self.pb2_directory.directories:
            self.index[entry.name].buildstream_object._recalculate_recursing_down(entry)

        if parent:
            self.ref = self.cas_cache.add_object(digest=parent.digest, buffer=self.pb2_directory.SerializeToString())
        else:
            self.ref = self.cas_cache.add_object(buffer=self.pb2_directory.SerializeToString())
        # We don't need to do anything more than that; files were already added ealier, and symlinks are
        # part of the directory structure.

    def _add_new_blank_directory(self, name) -> Directory:
        bst_dir = CasBasedDirectory(self.context, parent=self, filename=name)
        new_pb2_dirnode = self.pb2_directory.directories.add()
        new_pb2_dirnode.name = name
        # Calculate the hash for an empty directory
        if name in self.index:
            raise VirtualDirectoryError("Creating directory {} would overwrite an existing item in {}"
                                        .format(name, str(self)))
        new_pb2_directory = remote_execution_pb2.Directory()
        self.cas_cache.add_object(digest=new_pb2_dirnode.digest, buffer=new_pb2_directory.SerializeToString())
        self.index[name] = IndexEntry(new_pb2_dirnode, buildstream_object=bst_dir)
        return bst_dir

    def create_directory(self, name: str) -> Directory:
        """Creates a directory if it does not already exist. This does not
        cause an error if something exists; it will remove files and
        symlinks to files which have the same name in this
        directory. Symlinks to directories with the name 'name' are
        unaltered; it's assumed that the target of that symlink will
        be used.

        """
        existing_item = self.find_pb2_entry(name)
        if isinstance(existing_item, remote_execution_pb2.FileNode):
            # Directory imported over file with same name
            self.remove_item(name)
        elif isinstance(existing_item, remote_execution_pb2.SymlinkNode):
            # Directory imported over symlink with same source name
            if self.symlink_target_is_directory(existing_item):
                return # That's fine; any files in the source directory should end up at the target of the symlink.
            else:
                self.remove_item(name) # Symlinks to files get replaced
        return self.descend(name, create=True) # Creates the directory if it doesn't already exist.

    def find_pb2_entry(self, name):
        if name in self.index:
            return self.index[name].pb2_object
        return None

    def _add_directory(self, name):
        if name in self.index:
            newdir = self.index[name].buildstream_object
            if not isinstance(newdir, CasBasedDirectory):
                # TODO: This may not be an actual error; it may actually overwrite it
                raise VirtualDirectoryError("New directory {} in {} would overwrite existing non-directory of type {}"
                                            .format(name, str(self), type(newdir)))
            dirnode = self.find_pb2_entry(name)
        else:
            newdir = CasBasedDirectory(self.context, parent=self, filename=name)
            dirnode = self.pb2_directory.directories.add()

        dirnode.name = name

        # Calculate the hash for an empty directory
        new_directory = remote_execution_pb2.Directory()
        self.cas_cache.add_object(digest=dirnode.digest, buffer=new_directory.SerializeToString())
        self.index[name] = IndexEntry(dirnode, buildstream_object=newdir)
        return newdir

    def _add_new_file(self, basename, filename) -> None:
        filenode = self.pb2_directory.files.add()
        filenode.name = filename
        self.cas_cache.add_object(digest=filenode.digest, path=os.path.join(basename, filename))
        is_executable = os.access(os.path.join(basename, filename), os.X_OK)
        filenode.is_executable = is_executable
        self.index[filename] = IndexEntry(filenode, modified=(filename in self.index))

    def _add_new_link(self, basename, filename) -> None:
        existing_link = self.find_pb2_entry(filename)
        if existing_link:
            symlinknode = existing_link
        else:
            symlinknode = self.pb2_directory.symlinks.add()
        symlinknode.name = filename
        # A symlink node has no digest.
        symlinknode.target = os.readlink(os.path.join(basename, filename))
        self.index[filename] = IndexEntry(symlinknode, modified=(existing_link is not None))

    def delete_entry(self, name):
        for collection in [self.pb2_directory.files, self.pb2_directory.symlinks, self.pb2_directory.directories]:
            if name in collection:
                collection.remove(name)
        if name in self.index:
            del self.index[name]

    def descend(self, subdirectory_spec: List[str], create: bool = False) -> Directory:
        """Descend one or more levels of directory hierarchy and return a new
        Directory object for that directory.

        Arguments:
        * subdirectory_spec (list of strings): A list of strings which are all directory
          names.
        * create (boolean): If this is true, the directories will be created if
          they don't already exist.

        Note: At the moment, creating a directory by descending does
        not update this object in the CAS cache. However, performing
        an import_files() into a subdirectory of any depth obtained by
        descending from this object *will* cause this directory to be
        updated and stored.

        """

        # It's very common to send a directory name instead of a list and this causes
        # bizarre errors, so check for it here
        if not isinstance(subdirectory_spec, list):
            subdirectory_spec = [subdirectory_spec]
        if not subdirectory_spec:
            return self
        # Because of the way split works, it's common to get a list which begins with
        # an empty string. Detect these and remove them, then start again.
        if subdirectory_spec[0] == "":
            return self.descend(subdirectory_spec[1:], create)

        if subdirectory_spec[0] in self.index:
            entry1 = self.index[subdirectory_spec[0]].pb2_object
            entry2 = self.index[subdirectory_spec[0]].buildstream_object
            if isinstance(entry2, CasBasedDirectory):
                return entry2.descend(subdirectory_spec[1:], create)
            elif isinstance(entry1, remote_execution_pb2.SymlinkNode):
                # TODO: implement descend through symlink. To do this, we may need to look
                # upwards, or to the root.
                raise VirtualDirectoryError("Descend through symlink is not implemented")
            else:
                error = "Cannot descend into {}, which is a '{}' in the directory {}"
                raise VirtualDirectoryError(error.format(subdirectory_spec[0],
                                                         type(entry).__name__,
                                                         self))
        else:
            if create:
                newdir = self._add_new_blank_directory(subdirectory_spec[0])
                return newdir.descend(subdirectory_spec[1:], create)
            else:
                error = "No entry called '{}' found in {}. There are directories called {}."
                directory_list = ",".join([entry.name for entry in self.pb2_directory.directories])
                raise VirtualDirectoryError(error.format(subdirectory_spec[0], str(self),
                                                         directory_list))
        return None

    def find_root(self) -> Directory:
        """ Finds the root of this directory tree by following 'parent' until there is
        no parent. """
        if self.parent:
            return self.parent.find_root()
        else:
            return self

    def _resolve_symlink(self, symlink, chain=True):
        """ Resolve the symlink node given and return the file or directory it points at. Needs to return a CASBasedDirectory for directories, and a FileNode for files."""
        absolute = symlink.target.startswith(os.path.sep)
        if absolute:
            root = self.find_root()
        else:
            root = self
        directory = root
        components = symlink.target.split(os.path.sep)
        for c in components:
            if c == "..":
                directory = directory.parent
            else:
                item = self.find_pb2_entry(c)
                if item is None:
                    raise VirtualDirectoryErorr("Tried to resolve a broken symlink; source is '{}' and target is '{}'".format(symlink.source, symlink_node.target))
                elif isinstance(item, remote_execution_pb2.FileNode):
                    # TODO: if there are further things to descend in 'components', then we should throw an error here
                    return item
                elif isinstance(item, remote_execution_pb2.SymlinkNode):
                    raise VirtualDirectoryError("Chained symlinks are not supported yet")
                elif isinstance(item, remote_execution_pb2.DirectoryNode):
                    directory = directory.descend(c, create=True)
                else:
                    raise VirtualDirectoryError("Internal error: Object '{}' in '{}' is not a recognised type".format(c, self))
        return directory

    def _resolve_symlink_or_directory(self, name):
        """Used only by _import_files_from_directory. Tries to resolve a
        directory name or symlink name. 'name' must be an entry in this
        directory. It must be a single symlink or directory name, not a path
        separated by path separators. If it's an existing directory name, it
        just returns the Directory object for that. If it's a symlink, it will
        attempt to find the target of the symlink and return that as a
        Directory object.

        If a symlink target doesn't exist, it will attempt to create it
        as long as it's within this directory tree.
        """

        if isinstance(self.index[name].buildstream_object, Directory):
            return self.index[name].buildstream_object
        # OK then, it's a symlink
        symlink = self.find_pb2_entry(name)
        absolute = symlink.target.startswith(os.path.sep)
        if absolute:
            root = self.find_root()
        else:
            root = self
        directory = root
        components = symlink.target.split(os.path.sep)
        for c in components:
            if c == "..":
                directory = directory.parent
            else:
                directory = directory.descend(c, create=True)
        return directory

    def _check_replacement(self, name, path_prefix, fileListResult):
        """ Checks whether 'name' exists, and if so, whether we can overwrite it.
        If we can, add the name to 'overwritten_files' and delete the existing entry.
        Returns 'True' if the import should go ahead.
        fileListResult.overwritten and fileListResult.ignore are updated depending
        on the result. """
        existing_entry = self.find_pb2_entry(name)
        relative_pathname = os.path.join(path_prefix, name)
        if existing_entry is None:
            return True
        if (isinstance(existing_entry,
                       (remote_execution_pb2.FileNode, remote_execution_pb2.SymlinkNode))):
            self.delete_entry(name)
            fileListResult.overwritten.append(relative_pathname)
            return True
        elif isinstance(existing_entry, remote_execution_pb2.DirectoryNode):
            # If 'name' maps to a DirectoryNode, then there must be an entry in index
            # pointing to another Directory.
            if self.index[name].buildstream_object.is_empty():
                self.delete_entry(name)
                fileListResult.overwritten.append(relative_pathname)
                return True
            else:
                # We can't overwrite a non-empty directory, so we just ignore it.
                fileListResult.ignored.append(relative_pathname)
                return False
        raise VirtualDirectoryError("Entry '{}' is not a recognised file/link/directory and not None; it is {}"
                                    .format(name, type(existing_entry)))

    def _import_directory_recursively(self, directory_name, source_directory, remaining_path, path_prefix):
        """ _import_directory_recursively and _import_files_from_directory will be called alternately
        as a directory tree is descended. """
        if directory_name in self.index:
            subdir = self._resolve_symlink_or_directory(directory_name)
            if not isinstance(subdir, CasBasedDirectory):
                raise VirtualDirectoryErorr("Attempted to import '{}' over an existing thing in '{}', but the destination isn't a directory or symlink to one.".format(directory_name, self))

        else:
            subdir = self._add_directory(directory_name)
        new_path_prefix = os.path.join(path_prefix, directory_name)
        subdir_result = subdir._import_files_from_directory(os.path.join(source_directory, directory_name),
                                                            [os.path.sep.join(remaining_path)],
                                                            path_prefix=new_path_prefix)
        return subdir_result

    def _import_files_from_directory(self, source_directory, files, path_prefix=""):
        result = FileListResult()
        for entry in sorted(files):
            if entry == ".": continue
            split_path = entry.split(os.path.sep)
            # The actual file on the FS we're importing
            import_file = os.path.join(source_directory, entry)
            # The destination filename, relative to the root where the import started
            relative_pathname = os.path.join(path_prefix, entry)
            if len(split_path) > 1:
                directory_name = split_path[0]
                # Hand this off to the importer for that subdir. This will only do one file -
                # a better way would be to hand off all the files in this subdir at once.
                subdir_result = self._import_directory_recursively(directory_name, source_directory,
                                                                   split_path[1:], path_prefix)

                result.combine(subdir_result)
            elif os.path.islink(import_file):  # careful about ordering here, as some cases overlap
                if self._check_replacement(entry, path_prefix, result):
                    self._add_new_link(source_directory, entry)
                    result.files_written.append(relative_pathname)
            elif os.path.isdir(import_file):
                # A plain directory which already exists isn't a problem; just ignore it.
                if entry not in self.index:
                    self._add_directory(entry)
            elif os.path.isfile(import_file):
                if self._check_replacement(entry, path_prefix, result):
                    self._add_new_file(source_directory, entry)
                    result.files_written.append(relative_pathname)
        return result

    def _save(self, name):
        """ Saves this directory into the content cache as a named ref. This function is not
        currently in use, but may be useful later. """
        self._recalculate_recursing_up()
        self._recalculate_recursing_down()
        (rel_refpath, refname) = os.path.split(name)
        refdir = os.path.join(self.cas_directory, 'refs', 'heads', rel_refpath)
        refname = os.path.join(refdir, refname)

        if not os.path.exists(refdir):
            os.makedirs(refdir)
        with open(refname, "wb") as f:
            f.write(self.ref.SerializeToString())

    def find_updated_files(self, modified_directory, prefix=""):
        """Find the list of written and overwritten files that would result
        from importing 'modified_directory' into this one.  This does
        not change either directory. The reason this exists is for
        direct imports of cas directories into other ones, which can
        be done by simply replacing a hash, but we still need the file
        lists.

        """
        result = FileListResult()
        for entry in modified_directory.pb2_directory.directories:
            existing_dir = self.find_pb2_entry(entry.name)
            if existing_dir:
                updates_files = existing_dir.find_updated_files(modified_directory.descend(entry.name),
                                                                os.path.join(prefix, entry.name))
                result.combine(updated_files)
            else:
                for f in source_directory.descend(entry.name).list_relative_paths():
                    result.files_written.append(os.path.join(prefix, f))
                    # None of these can overwrite anything, since the original files don't exist
        for entry in modified_directory.pb2_directory.files + modified_directory.pb2_directory.symlinks:
            if self.find_pb2_entry(entry.name):
                result.files_overwritten.apppend(os.path.join(prefix, entry.name))
            result.file_written.apppend(os.path.join(prefix, entry.name))
        return result

    def files_in_subdir(sorted_files, dirname):
        """Filters sorted_files and returns only the ones which have
           'dirname' as a prefix, with that prefix removed.

        """
        if not dirname.endswith(os.path.sep):
            dirname += os.path.sep
        return [f[len(dirname):] for f in sorted_files if f.startswith(dirname)]

    def symlink_target_is_directory(self, symlink_node):
        x = self._resolve_symlink(symlink_node)
        return isinstance(x, CasBasedDirectory)

    def _partial_import_cas_into_cas(self, source_directory, files, path_prefix="", file_list_required=True):
        """ Import only the files and symlinks listed in 'files' from source_directory to this one.
        Args:
           source_directory (:class:`.CasBasedDirectory`): The directory to import from
           files ([str]): List of pathnames to import.
           path_prefix (str): Prefix used to add entries to the file list result.
           file_list_required: Whether to update the file list while processing.
        """

        result = FileListResult()
        processed_directories = set()
        for f in files:
            if f == ".": continue
            fullname = os.path.join(path_prefix, f)
            components = f.split(os.path.sep)
            if len(components)>1:
                # We are importing a thing which is in a subdirectory. We may have already seen this dirname
                # for a previous file.
                dirname = components[0]
                if dirname not in processed_directories:
                    # Now strip off the first directory name and import files recursively.
                    subcomponents = CasBasedDirectory.files_in_subdir(files, dirname)
                    self.create_directory(dirname)
                    dest_subdir = self.descend(dirname)
                    src_subdir = source_directory.descend(dirname)
                    import_result = dest_subdir._partial_import_cas_into_cas(src_subdir, subcomponents,
                                                                             path_prefix=fullname, file_list_required=file_list_required)
                    result.combine(import_result)
                processed_directories.add(dirname)
            elif isinstance(source_directory.index[f].buildstream_object, CasBasedDirectory):
                # The thing in the input file list is a directory on its own. In which case, replace any existing file, or symlink to file
                # with the new, blank directory - if it's neither of those things, or doesn't exist, then just create the dir.
                self.create_directory(f)
            else:
                # We're importing a file or symlink - replace anything with the same name.
                self._check_replacement(f, path_prefix, result)
                item = source_directory.index[f].pb2_object
                if isinstance(item, remote_execution_pb2.FileNode):
                    filenode = self.pb2_directory.files.add(digest=item.digest, name=f,
                                                            is_executable=item.is_executable)
                    self.index[f] = IndexEntry(filenode, modified=(fullname in result.overwritten))
                else:
                    assert(isinstance(item, remote_execution_pb2.SymlinkNode))
                    symlinknode = self.pb2_directory.symlinks.add(name=f, target=item.target)
                    # A symlink node has no digest.
                    self.index[f] = IndexEntry(symlinknode, modified=(fullname in result.overwritten))
        return result

    def transfer_node_contents(destination, source):
        """Transfers all fields from the source PB2 node into the
        destination. Destination and source must be of the same type and must
        be a FileNode, SymlinkNode or DirectoryNode.
        """
        assert(type(destination) == type(source))
        destination.name = source.name
        if isinstance(destination, remote_execution_pb2.FileNode):
            destination.digest.hash = source.digest.hash
            destination.digest.size_bytes = source.digest.size_bytes
            destination.is_executable = source.is_executable
        elif isinstance(destination, remote_execution_pb2.SymlinkNode):
            destination.target = source.target
        elif isinstance(destination, remote_execution_pb2.DirectoryNode):
            destination.digest.hash = source.digest.hash
            destination.digest.size_bytes = source.digest.size_bytes
        else:
            raise VirtualDirectoryError("Incompatible type '{}' used as destination for transfer_node_contents"
                                        .format(destination.type))

    def _add_directory_from_node(self, source_node):
        # Duplicate the given node and add it to our index with a CasBasedDirectory object.
        # No existing entry with the source node's name can exist.
        assert(self.find_pb_entry(source_node.name) is None)
        new_dir_node = self.pb2_directory.directories.add()
        CasBasedDirectory.transfer_node_contents(new_dir_node, source_node)
        buildStreamDirectory = CasBasedDirectory(self.context, ref=source_node.digest,
                                                 parent=self, filename=source_node.name)
        self.index[source_node.name] = IndexEntry(source_node, buildstream_object=buildStreamDirectory, modified=True)

    def _full_import_cas_into_cas(self, source_directory, path_prefix="", file_list_required=True):
        """ Import all files and symlinks from source_directory to this one.
        Args:
           source_directory (:class:`.CasBasedDirectory`): The directory to import from
           path_prefix (str): Prefix used to add entries to the file list result.
           file_list_required: Whether to update the file list while processing.
        """

        result = FileListResult()

        # First, deal with directories.
        for entry in source_directory.pb2_directory.directories:
            existing_item = self.find_pb2_entry(entry.name)
            if existing_item:
                self.create_directory(entry.name) # Handles existing stuff
                src_dir = source_directory.descend(entry.name)
                dest_dir = self.descend(entry.name)
                subdir_results = dest_dir._full_import_cas_into_cas(src_dir, os.path.join(path_prefix, entry.name), file_list_required=file_list_required)
                result.combine(subdir_results)
            else:
                # If there was no existing item, we don't need to recurse - just add the directory in as is.

                # TODO: Although this is intended to be a quick import, it causes a full population with _populate_index, which will recurse through the
                # new directory, causing a duplication in memory. Realistically, do we need to create the subdir's CasBasedDirectory object *on creation* or can we wait to descend? Same problem for all cases, really.
                self._add_directory_from_node(entry)
                # We still need to add all the new paths.
                result.files_written.extend(self.descend(entry.name,create=True).list_relative_paths())
        for collection in ('files', 'symlinks'):
            for entry in getattr(source_directory.pb2_directory, collection):
                # TODO: Note that this and the symlinks case are now almost identical
                existing_item = self.find_pb2_entry(entry.name)
                relative_pathname = os.path.join(path_prefix, entry.name)
                if existing_item:
                    node = existing_item
                    result.files_overwritten.append(relative_pathname)
                else:
                    node = getattr(self.pb2_directory, collection).add()
                CasBasedDirectory.transfer_node_contents(node, entry)
                self.index[entry.name] = IndexEntry(node, modified=(existing_item is not None))
                result.files_written.append(relative_pathname)
        return result

    def _import_cas_into_cas(self, source_directory, files=None):
        """ A full import is significantly quicker than a partial import, because we can just
        replace one directory with another's hash, without doing any recursion.
        """
        if files is None:
            return self._full_import_cas_into_cas(source_directory)
            files = source_directory.list_relative_paths()
            print("Extracted all files from source directory '{}': {}".format(source_directory, files))
        return self._partial_import_cas_into_cas(source_directory, files)

    def import_files(self, external_pathspec: any, files: List[str] = None,
                     report_written: bool = True, update_utimes: bool = False,
                     can_link: bool = False) -> FileListResult:
        """Imports some or all files from external_path into this directory.

        Keyword arguments: external_pathspec: Either a string
        containing a pathname, or a Directory object, to use as the
        source.

        files (list of strings): A list of all the files relative to
        the external_pathspec to copy. If 'None' is supplied, all
        files are copied.

        report_written (bool): Return the full list of files
        written. Defaults to true. If false, only a list of
        overwritten files is returned.

        update_utimes (bool): Currently ignored, since CAS does not store utimes.

        can_link (bool): Ignored, since hard links do not have any meaning within CAS.
        """

        duplicate_cas = None
        if isinstance(external_pathspec, CasBasedDirectory):
            result = self._import_cas_into_cas(external_pathspec, files=files)

            # Duplicate the current directory and do an import that way.
            duplicate_cas = CasBasedDirectory(self.context, ref=self.ref)
            with tempfile.TemporaryDirectory(prefix="roundtrip") as tmpdir:
                external_pathspec.export_files(tmpdir)
                if files is None:
                    files = list_relative_paths(tmpdir)
                duplicate_cas._import_files_from_directory(tmpdir, files=files)
                duplicate_cas._recalculate_recursing_down()
                if duplicate_cas.parent:
                    duplicate_cas.parent._recalculate_recursing_up(self)
        else:
            if isinstance(external_pathspec, FileBasedDirectory):
                source_directory = external_pathspec.get_underlying_directory()
            else:
                source_directory = external_pathspec
            if files is None:
                files = list_relative_paths(external_pathspec)
            result = self._import_files_from_directory(source_directory, files=files)

        # TODO: No notice is taken of report_written, update_utimes or can_link.
        # Current behaviour is to fully populate the report, which is inefficient,
        # but still correct.

        # We need to recalculate and store the hashes of all directories both
        # up and down the tree; we have changed our directory by importing files
        # which changes our hash and all our parents' hashes of us. The trees
        # lower down need to be stored in the CAS as they are not automatically
        # added during construction.
        self._recalculate_recursing_down()
        if self.parent:
            self.parent._recalculate_recursing_up(self)
        if duplicate_cas:
            if duplicate_cas.ref.hash != self.ref.hash:
                print("Mismatch between file-imported result {} and cas-to-cas imported result {}.".format(duplicate_cas.ref.hash,self.ref.hash))

        return result

    def set_deterministic_mtime(self) -> None:
        """ Sets a static modification time for all regular files in this directory.
        Since we don't store any modification time, we don't need to do anything.
        """
        pass

    def set_deterministic_user(self) -> None:
        """ Sets all files in this directory to the current user's euid/egid.
        We also don't store user data, so this can be ignored.
        """
        pass

    def export_files(self, to_directory: str, can_link: bool = False, can_destroy: bool = False) -> None:
        """Copies everything from this into to_directory.

        Arguments:

        to_directory (string): a path outside this directory object
        where the contents will be copied to.

        can_link (bool): Whether we can create hard links in to_directory
        instead of copying.

        """
        if not os.path.exists(to_directory):
            os.mkdir(to_directory)

        for entry in self.pb2_directory.directories:
            if entry.name not in self.index:
                raise VirtualDirectoryError("CasDir {} contained '{}' in directories but not in the index"
                                            .format(str(self), entry.name))
            if not self._directory_read:
                raise VirtualDirectoryError("CasDir {} has not been indexed yet".format(str(self)))
            dest_dir = os.path.join(to_directory, entry.name)
            if not os.path.exists(dest_dir):
                os.mkdir(dest_dir)
            target = self.descend([entry.name])
            target.export_files(dest_dir)
        for entry in self.pb2_directory.files:
            # Extract the entry to a single file
            dest_name = os.path.join(to_directory, entry.name)
            src_name = self.cas_cache.objpath(entry.digest)
            safe_copy(src_name, dest_name)
            if entry.is_executable:
                os.chmod(dest_name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR |
                         stat.S_IRGRP | stat.S_IXGRP |
                         stat.S_IROTH | stat.S_IXOTH)
        for entry in self.pb2_directory.symlinks:
            src_name = os.path.join(to_directory, entry.name)
            target_name = entry.target
            try:
                os.symlink(target_name, src_name)
            except FileExistsError as e:
                raise BstError(("Cannot create a symlink named {} pointing to {}." +
                                " The Original error was: {}").
                               format(src_name, entry.target, e))

    def is_empty(self) -> bool:
        """ Return true if this directory has no files, subdirectories or links in it.
        """
        return len(self.index) == 0

    def mark_unmodified(self) -> None:
        """ Marks all files in this directory (recursively) as unmodified.
        """
        # TODO: We don't actually mark ourselves unmodified here, because we can't
        # get to the containing IndexEntry.
        for i in self.index.values():
            i.modified = False
            if isinstance(i.buildstream_object, CasBasedDirectory):
                i.buildstream_object.mark_unmodified()

    def list_modified_paths(self) -> List[str]:
        """Provide a list of relative paths which have been modified since the
        last call to mark_unmodified. Does not include directory objects.

        Return value: List(str) - list of modified paths
        """

        filelist = []
        for (k, v) in self.index.items():
            if isinstance(v.buildstream_object, CasBasedDirectory):
                filelist.extend([k + "/" + x for x in v.buildstream_object.list_relative_paths()])
            elif isinstance(v.pb2_object, remote_execution_pb2.FileNode) and v.modified:
                filelist.append(k)
        return filelist

    def list_relative_paths(self) -> List[str]:
        """Provide a list of all relative paths.

        Return value: List(str) - list of all paths
        """

        filelist = []
        for (k, v) in self.index.items():
            if isinstance(v.buildstream_object, CasBasedDirectory):
                filelist.append(k)
                filelist.extend([k + "/" + x for x in v.buildstream_object.list_relative_paths()])
            elif isinstance(v.pb2_object, remote_execution_pb2.FileNode):
                filelist.append(k)
            elif isinstance(v.pb2_object, remote_execution_pb2.SymlinkNode):
                filelist.append(k)
        return filelist

    def _get_identifier(self) -> str:
        path = ""
        if self.parent:
            path = self.parent._get_identifier()
        if self.filename:
            path += "/" + self.filename
        else:
            path += "/" + self.common_name
        return path

    def __str__(self) -> str:
        return "[CAS:{}]".format(self._get_identifier())

    def get_underlying_directory(self) -> str:
        """ Returns the underlying (real) file system directory this
        object refers to. This will throw an exception if there isn't
        a real directory behind the object. """
        raise VirtualDirectoryError("get_underlying_directory was called on a CAS-backed directory," +
                                    " which has no underlying directory.")
