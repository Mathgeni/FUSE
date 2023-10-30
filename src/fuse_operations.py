import errno
import os
import pdb
import stat as stat_m

import pyfuse3
from pyfuse3 import FUSEError

from collections import defaultdict
from src._constants import STAT_ATTRS, PARENT_DIRS, FILE_ATTRS


class Operations(pyfuse3.Operations):

    enable_writeback_cache = True

    def __init__(self):
        super().__init__()
        self._inode_path_map: dict[int, str | set] = {
            pyfuse3.ROOT_INODE: '/home/mathgeni/fsfiles',
            pyfuse3.ROOT_INODE + 1: '/home/mathgeni/fsfiles_replica'
        }
        self._lookup_cnt: defaultdict[int, int] = defaultdict(lambda: 0)
        self._fd_inode_map: dict[int, int] = dict()
        self._inode_fd_map: dict[int, int] = dict()
        self._fd_open_count: dict[int, int] = dict()

    @staticmethod
    async def _getattr(path=None, fd=None):
        try:
            if fd is None:
                stat = os.lstat(path)
            else:
                stat = os.fstat(fd)
        except OSError as exc:
            raise FUSEError(exc.errno)

        entry = pyfuse3.EntryAttributes()
        for attr in STAT_ATTRS:
            setattr(entry, attr, getattr(stat, attr))
        entry.generation = 0
        entry.entry_timeout = 0
        entry.attr_timeout = 0
        entry.st_blksize = 512
        entry.st_blocks = (entry.st_size + entry.st_blksize - 1) // entry.st_blksize
        return entry

    async def _inode_to_path(self, inode):
        try:
            val = self._inode_path_map[inode]
        except KeyError:
            raise FUSEError(errno.ENOENT)

        if isinstance(val, set):
            val = next(iter(val))
        return val

    async def _add_path(self, inode, path):
        self._lookup_cnt[inode] += 1

        if inode not in self._inode_path_map:
            self._inode_path_map[inode] = path
            return

        val = self._inode_path_map[inode]
        if isinstance(val, set):
            val.add(path)
        elif val != path:
            self._inode_path_map[inode] = {path, val}

    async def forget(self, inode_list):
        for (inode, nlookup) in inode_list:
            if self._lookup_cnt[inode] > nlookup:
                self._lookup_cnt[inode] -= nlookup
                continue
            assert inode not in self._inode_fd_map
            del self._lookup_cnt[inode]
            try:
                del self._inode_path_map[inode]
            except KeyError:  # may have been deleted
                pass

    async def lookup(self, inode_p, name, ctx=None):
        name = os.fsdecode(name)
        path = os.path.join(await self._inode_to_path(inode_p), name)
        attr = await self._getattr(path=path)
        if name not in PARENT_DIRS:
            await self._add_path(attr.st_ino, path)
        return attr

    async def getattr(self, inode, ctx=None):
        if inode in self._inode_fd_map:
            return await self._getattr(fd=self._inode_fd_map[inode])
        else:
            return await self._getattr(path=await self._inode_to_path(inode))

    async def readlink(self, inode, ctx):
        path = await self._inode_to_path(inode)
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise FUSEError(exc.errno)
        return os.fsencode(target)

    async def opendir(self, inode, ctx):
        return inode

    async def readdir(self, inode, off, token):
        paths = [await self._inode_to_path(inode)]
        if inode == pyfuse3.ROOT_INODE:
            paths.append(await self._inode_to_path(inode + 1))
        entries = []
        for path in paths:
            for name in os.listdir(path):
                if name in PARENT_DIRS:
                    continue
                attr = await self._getattr(path=os.path.join(path, name))
                entries.append((attr.st_ino, name, attr, paths.index(path)))

        for (ino, name, attr, index) in sorted(entries):
            if ino <= off:
                continue
            if not pyfuse3.readdir_reply(
                    token, os.fsencode(name), attr, ino):
                break
            await self._add_path(attr.st_ino, os.path.join(paths[index], name))

    async def unlink(self, inode_p, name, ctx):
        name = os.fsdecode(name)
        parent = await self._inode_to_path(inode_p)
        path = os.path.join(parent, name)
        try:
            inode = os.lstat(path).st_ino
            os.unlink(path)
        except OSError as exc:
            raise FUSEError(exc.errno)
        if inode in self._lookup_cnt:
            await self._forget_path(inode, path)

    async def rmdir(self, inode_p, name, ctx):
        name = os.fsdecode(name)
        parent = await self._inode_to_path(inode_p)
        path = os.path.join(parent, name)
        try:
            inode = os.lstat(path).st_ino
            os.rmdir(path)
        except OSError as exc:
            raise FUSEError(exc.errno)
        if inode in self._lookup_cnt:
            await self._forget_path(inode, path)

    async def _forget_path(self, inode, path):
        val = self._inode_path_map[inode]
        if isinstance(val, set):
            val.remove(path)
            if len(val) == 1:
                self._inode_path_map[inode] = next(iter(val))
        else:
            del self._inode_path_map[inode]

    async def symlink(self, inode_p, name, target, ctx):
        name = os.fsdecode(name)
        target = os.fsdecode(target)
        parent = await self._inode_to_path(inode_p)
        path = os.path.join(parent, name)
        try:
            os.symlink(target, path)
            os.chown(path, ctx.uid, ctx.gid, follow_symlinks=False)
        except OSError as exc:
            raise FUSEError(exc.errno)
        stat = os.lstat(path)
        await self._add_path(stat.st_ino, path)
        return await self.getattr(stat.st_ino)

    async def rename(self, inode_p_old, name_old, inode_p_new, name_new, flags, ctx):
        if flags != 0:
            raise FUSEError(errno.EINVAL)

        name_old = os.fsdecode(name_old)
        name_new = os.fsdecode(name_new)
        parent_old = await self._inode_to_path(inode_p_old)
        parent_new = await self._inode_to_path(inode_p_new)
        path_old = os.path.join(parent_old, name_old)
        path_new = os.path.join(parent_new, name_new)
        try:
            os.rename(path_old, path_new)
            inode = os.lstat(path_new).st_ino
        except OSError as exc:
            raise FUSEError(exc.errno)
        if inode not in self._lookup_cnt:
            return

        val = self._inode_path_map[inode]
        if isinstance(val, set):
            assert len(val) > 1
            val.add(path_new)
            val.remove(path_old)
        else:
            assert val == path_old
            self._inode_path_map[inode] = path_new

    async def link(self, inode, new_inode_p, new_name, ctx):
        new_name = os.fsdecode(new_name)
        parent = await self._inode_to_path(new_inode_p)
        path = os.path.join(parent, new_name)
        try:
            os.link(await self._inode_to_path(inode), path, follow_symlinks=False)
        except OSError as exc:
            raise FUSEError(exc.errno)
        await self._add_path(inode, path)
        return await self.getattr(inode)

    async def setattr(self, inode, attr, fields, fh, ctx):
        if fh is None:
            path_or_fh = await self._inode_to_path(inode)
            truncate = os.truncate
            chmod = os.chmod
            chown = os.chown
            stat = os.lstat
        else:
            path_or_fh = fh
            truncate = os.ftruncate
            chmod = os.fchmod
            chown = os.fchown
            stat = os.fstat

        try:
            if fields.update_size:
                truncate(path_or_fh, attr.st_size)

            if fields.update_mode:
                chmod(path_or_fh, stat_m.S_IMODE(attr.st_mode))

            if fields.update_uid:
                chown(path_or_fh, attr.st_uid, -1, follow_symlinks=False)

            if fields.update_gid:
                chown(path_or_fh, -1, attr.st_gid, follow_symlinks=False)

            if fields.update_atime and fields.update_mtime:
                if fh is None:
                    os.utime(path_or_fh, None, follow_symlinks=False,
                             ns=(attr.st_atime_ns, attr.st_mtime_ns))
                else:
                    os.utime(path_or_fh, None,
                             ns=(attr.st_atime_ns, attr.st_mtime_ns))
            elif fields.update_atime or fields.update_mtime:
                oldstat = stat(path_or_fh)
                if not fields.update_atime:
                    attr.st_atime_ns = oldstat.st_atime_ns
                else:
                    attr.st_mtime_ns = oldstat.st_mtime_ns
                if fh is None:
                    os.utime(path_or_fh, None, follow_symlinks=False,
                             ns=(attr.st_atime_ns, attr.st_mtime_ns))
                else:
                    os.utime(path_or_fh, None,
                             ns=(attr.st_atime_ns, attr.st_mtime_ns))

        except OSError as exc:
            raise FUSEError(exc.errno)

        return await self.getattr(inode)

    async def mknod(self, inode_p, name, mode, rdev, ctx):
        path = os.path.join(await self._inode_to_path(inode_p), os.fsdecode(name))
        try:
            os.mknod(path, mode=(mode & ~ctx.umask), device=rdev)
            os.chown(path, ctx.uid, ctx.gid)
        except OSError as exc:
            raise FUSEError(exc.errno)
        attr = await self._getattr(path=path)
        await self._add_path(attr.st_ino, path)
        return attr

    async def mkdir(self, inode_p, name, mode, ctx):
        path = os.path.join(await self._inode_to_path(inode_p), os.fsdecode(name))
        try:
            os.mkdir(path, mode=(mode & ~ctx.umask))
            os.chown(path, ctx.uid, ctx.gid)
        except OSError as exc:
            raise FUSEError(exc.errno)
        attr = await self._getattr(path=path)
        await self._add_path(attr.st_ino, path)
        return attr

    async def statfs(self, ctx):
        root = self._inode_path_map[pyfuse3.ROOT_INODE]
        stat_ = pyfuse3.StatvfsData()
        try:
            statfs = os.statvfs(root)
        except OSError as exc:
            raise FUSEError(exc.errno)
        for attr in FILE_ATTRS:
            setattr(stat_, attr, getattr(statfs, attr))
        stat_.f_namemax = statfs.f_namemax - (len(root) + 1)
        return stat_

    async def open(self, inode, flags, ctx):
        if inode in self._inode_fd_map:
            fd = self._inode_fd_map[inode]
            self._fd_open_count[fd] += 1
            return pyfuse3.FileInfo(fh=fd)
        try:
            fd = os.open(await self._inode_to_path(inode), flags)
        except OSError as exc:
            raise FUSEError(exc.errno)
        self._inode_fd_map[inode] = fd
        self._fd_inode_map[fd] = inode
        self._fd_open_count[fd] = 1
        return pyfuse3.FileInfo(fh=fd)

    async def create(self, inode_p, name, mode, flags, ctx):
        path = os.path.join(await self._inode_to_path(inode_p), os.fsdecode(name))
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_TRUNC)
        except OSError as exc:
            raise FUSEError(exc.errno)
        attr = await self._getattr(fd=fd)
        await self._add_path(attr.st_ino, path)
        self._inode_fd_map[attr.st_ino] = fd
        self._fd_inode_map[fd] = attr.st_ino
        self._fd_open_count[fd] = 1
        return pyfuse3.FileInfo(fh=fd), attr

    async def read(self, fd, offset, length):
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, length)

    async def write(self, fd, offset, buf):
        os.lseek(fd, offset, os.SEEK_SET)
        return os.write(fd, buf)

    async def release(self, fd):
        if self._fd_open_count[fd] > 1:
            self._fd_open_count[fd] -= 1
            return

        del self._fd_open_count[fd]
        inode = self._fd_inode_map[fd]
        del self._inode_fd_map[inode]
        del self._fd_inode_map[fd]
        try:
            os.close(fd)
        except OSError as exc:
            raise FUSEError(exc.errno)
