import os
import asyncio
import logging
import faulthandler
import aiofiles
import aiofiles.os as ios

import pyfuse3
import pyfuse3_asyncio

log = logging.getLogger(__name__)
pyfuse3_asyncio.enable()


class TestFUSE(pyfuse3.Operations):

    def __init__(self, source: str) -> None:
        super().__init__()
        self._inode_path = {pyfuse3.ROOT_INODE: source}

    async def _path(self, partial: str) -> str:
        partial = partial[1:] if partial.startswith('/') else partial
        path = os.path.join(self._root, partial)
        return path

    async def getattr(self, path: str, ctx=None):
        path = self._path(path)
        stats = await ios.stat(path)
        return stats


def main():
    testfs = TestFUSE('home/mathgeni/')
    fuse_options = set(pyfuse3.default_options)
    fuse_options.add('fsname=hello')
    pyfuse3.init(testfs, '/home/mathgeni/', fuse_options)
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(pyfuse3.main())
    finally:
        loop.close()
    pyfuse3.close()


if __name__ == '__main__':
    main()