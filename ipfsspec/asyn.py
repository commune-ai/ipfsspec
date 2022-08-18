import io
import time
import weakref
import copy
import asyncio
import aiohttp
from fsspec.asyn import _run_coros_in_chunks
from fsspec.utils import is_exception
from fsspec.callbacks import _DEFAULT_CALLBACK
from glob import has_magic
# from .buffered_file import IPFSBufferedFile
import json
import tempfile 
from copy import deepcopy
from fsspec.asyn import AsyncFileSystem, sync, sync_wrapper
from ipfshttpclient.multipart import stream_directory, stream_files #needed to prepare files/directory to be sent through http
import os
from fsspec.exceptions import FSTimeoutError
from fsspec.implementations.local import LocalFileSystem
from fsspec.spec import AbstractBufferedFile
from fsspec.utils import is_exception, other_paths
from fsspec.implementations.local import make_path_posix

# import sys,os
# sys.path.append(os.getenv('PWD'))
from ipfsspec.gateway import MultiGateway, AsyncIPFSGateway
from ipfsspec.utils import dict_get, dict_put, dict_hash,dict_equal
import streamlit as st
import logging

logger = logging.getLogger("ipfsspec")


DEFAULT_GATEWAY = None

import requests
from requests.exceptions import HTTPError

IPFSHTTP_LOCAL_HOST = os.getenv('IPFSHTTP_LOCAL_HOST', '127.0.0.1')


GATEWAY_MAP = {
    'infura': ['https://ipfs.infura.io:5001'],
    'local': [f'http://{IPFSHTTP_LOCAL_HOST}:8080'],
    'public': ["https://ipfs.io",
               "https://gateway.pinata.cloud",
               "https://cloudflare-ipfs.com",
               "https://dweb.link"]
}

class AsyncIPFSFileSystem(AsyncFileSystem):
    sep = "/"
    protocol = "ipfs"
    root = '/tmp/fspec/ipfs'

    def __init__(self, asynchronous=False,
                 gateway_type='local',
                loop=None, 
                root = None,
                
                client_kwargs={},
                 **storage_options):
        super().__init__(self, asynchronous=asynchronous, loop=loop, **storage_options,)
        self._session = None
        self.client_kwargs=client_kwargs
        self.gateway = None
        self.change_gateway_type = gateway_type

        self.local_fs = AsyncFileSystem(loop=loop)
        if root:
            self.root = root

        if not asynchronous:
            sync(self.loop, self.set_session, timeout=3600)

    @property
    def change_gateway_type(self):
        return self.gateway_type

    @change_gateway_type.setter    
    def change_gateway_type(self, value):
        self.gateway_type = value
        self.gateway = MultiGateway([AsyncIPFSGateway(url=url, gateway_type=self.gateway_type) for url in GATEWAY_MAP[self.gateway_type]])
        print(f"Changed to {self.gateway_type} node")

    # @property
    # def gateway(self, gateway_type = None):
    #     if gateway_type is None:
    #         gateway_type = self.gateway_type
    #     return MultiGateway.get_gateway(gateway_type=self.gateway_type)

    @staticmethod
    async def get_client(**kwargs):
        timeout = aiohttp.ClientTimeout(sock_connect=1, sock_read=5)
        kwargs = {"timeout": timeout, **kwargs}
        return aiohttp.ClientSession(**kwargs)


    @staticmethod
    def close_session(loop, session):
        if loop is not None and loop.is_running():
            try:
                sync(loop, session.close, timeout=0.1)
                return
            except (TimeoutError, FSTimeoutError):
                pass
        if session._connector is not None:
            # close after loop is dead
            session._connector._close()

    async def set_session(self, refresh=False):
        if (not self._session) or (refresh==True):
            self._session = await self.get_client(loop=self.loop, **self.client_kwargs)
            if not self.asynchronous:
                weakref.finalize(self, self.close_session, self.loop, self._session)
        return self._session
    
    async def close(self):
        """Close file

        Finalizes writes, discards cache
        """
        if getattr(self, "_unclosable", False):
            return
        if self.closed:
            return
        if self.mode == "rb":
            self.cache = None
        else:
            if not self.forced:
                await self.flush(force=True)

            if self.fs is not None:
                self.fs.invalidate_cache(self.path)
                self.fs.invalidate_cache(self.fs._parent(self.path))

        self.closed = True

    
    def __del__(self):
        self.close_session(loop=self.loop, session=self._session)

    async def _expand_path(self, path, recursive=False, maxdepth=None):
        if isinstance(path, str):
            out = await self._expand_path([path], recursive, maxdepth)
        else:
            # reduce depth on each recursion level unless None or 0
            maxdepth = maxdepth if not maxdepth else maxdepth - 1
            out = set()
            path = [self._strip_protocol(p) for p in path]
            for p in path:  # can gather here
                if has_magic(p):
                    bit = set(await self._glob(p))
                    out |= bit
                    if recursive:
                        out |= set(
                            await self._expand_path(
                                list(bit), recursive=recursive, maxdepth=maxdepth
                            )
                        )
                    continue
                elif recursive:
                    
                    rec = set(await self._find(p, maxdepth=maxdepth, withdirs=True))
                    out |= rec

                if p not in out and (recursive is False or (await self._exists(p))):
                    # should only check once, for the root
                    out.add(p)
        if not out:
            raise FileNotFoundError(path)
        return list(sorted(out))

    async def _rm_file(self ,path, gc=True, **kwargs):
        session = await self.set_session()
        response = await self.gateway.api_post(session=session, endpoint='files/rm', recursive='true', arg=path)
        if gc:
            await self.gateway.api_post(session=session, endpoint='repo/gc')
        return path

    # async def _rm(self, path, recursion=True , gc=True,**kwargs):
    #     recursion='true' if recursion else 'false'
    #     session = await self.set_session()
    #     await self.gateway.api_post(session=session, endpoint='files/rm', recursion=recursion, arg=path)
    #     if gc:
    #         await self.gateway.api_post(session=session, endpoint='repo/gc')

    @staticmethod
    def ensure_path(path):
        assert isinstance(path, str), f'path must be string, but got {path}'
        if len(path) == 0:
            path = '/'
        elif len(path) > 0:
            if path[0] != '/':
                path = '/' + path
        
        return path


    async def  _stat(self, path):
        session = await self.set_session()

        res = await self.gateway.api_get(endpoint='files/stat', session=session, path=path)
        return res
    stat = sync_wrapper(_stat)

    async def _ls(self, path='/', detail=True, recursive=False, **kwargs):
        # path = self._strip_protocol(path)

        session = await self.set_session()
        res = await self.gateway.ls(session=session, path=path)

        if recursive:
            # this is prob not needed with self.find
            res_list = []
            cor_list = []
            for r in res:
                if r['type'] == 'directory':
                    cor_list.append(self._ls(path=r['name'], detail=True, recursive=recursive, **kwargs))
                elif r['type'] == 'file':
                    res_list += [r]
                    
            if len(cor_list) > 0:
                for r in (await asyncio.gather(*cor_list)):
                    res_list += r
            res = res_list


        if detail:
            return res
        else:
            return [r["name"] for r in res]

    ls = sync_wrapper(_ls)

    # def _store_path(self, path, hash):



    async def _is_pinned(self, cid):
        session = await self.set_session()
        res = await self.gateway.api_post(endpoint='pin/ls', session=session, params={'arg':cid})
        pinned_cid_list = list(json.loads(res.decode()).get('Keys').keys())
        return bool(cid in pinned_cid_list)

    is_pinned = sync_wrapper(_is_pinned)


    async def _ls_pinned(self):
        session = await self.set_session()
        res = await self.gateway.api_post(endpoint='pin/ls', session=session, params={})

    ls_pinned = sync_wrapper(_ls_pinned)

    async def _pin(self, cid, recursive=False, progress=False):
        session = await self.set_session()
        res = await self.gateway.api_post(endpoint='pin/add', session=session, params={'arg':cid, 
                                                                     'recursive': recursive,
                                                                      'progress': progress})
        return bool(cid in pinned_cid_list)

    pin = sync_wrapper(_pin)


    async def _api_post(self, endpoint, **kwargs):
        session = await self.set_session()
        return await self.gateway.api_post(endpoint=endpoint, session=session, **kwargs)
    api_post = sync_wrapper(_api_post)

    async def _api_get(self, endpoint, **kwargs):
        session = await self.set_session()
        res =  await self.gateway.api_get(endpoint=endpoint, session=session, **kwargs)
        if res.headers['Content-Type'] == 'application/json':
            res = await res.json()
        elif res.headers['Content-Type'] == 'text/plain':
            res = json.loads((await res.content.read()).decode())
        return res
    api_get = sync_wrapper(_api_get)

    async def _cp(self,path1, path2):
        session = await self.set_session()

        res = await self.gateway.cp(session=session, arg=[path1, path2])
        return res
    cp = sync_wrapper(_cp)

    async def _put_file(self,
        lpath=None,
        rpath=None,
        pin=True,
        chunker=262144, 
        wrap_with_directory=False,
        **kwargs
    ):

        if 'path' in kwargs:
            lpath = kwargs.pop('path')

        session = await self.set_session()
        if not os.path.isfile(lpath): raise TypeError ('Use `put` to upload a directory')        
        if self.gateway_type == 'public': raise TypeError ('`put_file` and `put` functions require local/infura `gateway_type`')
        

    

        params = {}
        params['wrap-with-directory'] = 'true' if wrap_with_directory else 'false'
        params['chunker'] = f'size-{chunker}'
        params['pin'] = 'true' if pin else 'false'
        
        data, headers = stream_files(lpath, chunk_size=chunker)
        data = self.data_gen_wrapper(data=data)                                        
        res = await self.gateway.api_post(endpoint='add', session=session,  params=params, data=data, headers=headers)

        res =  await res.json()
        if rpath:

            # await self._cp(path1=f'/ipfs/{res["Hash"]}', path2=rpath )
            rdir = os.path.dirname(rpath)
            # await self._mkdir(path=rdir, create_parents=True)

            cid_hash = res["Hash"]

            ipfs_path = f'/ipfs/{cid_hash}'

            
            await self._cp(ipfs_path, rpath)
            # print(await self._exists(rpath),await self._size(rpath), await self._info(ipfs_path))
            print(await self._exists(rpath), rpath)



        return res
        # return res
    
    put_file = sync_wrapper(_put_file)
    @staticmethod
    async def data_gen_wrapper(data):
        for d in data:
            yield d

    # async def _put(
    #     self,
    #     lpath,
    #     rpath='/tmp',
    #     recursive=False,
    #     callback=_DEFAULT_CALLBACK,
    #     batch_size=None,
    #     **kwargs,
    # ):
    #     """Copy file(s) from local.

    #     Copies a specific file or tree of files (if recursive=True). If rpath
    #     ends with a "/", it will be assumed to be a directory, and target files
    #     will go within.

    #     The put_file method will be called concurrently on a batch of files. The
    #     batch_size option can configure the amount of futures that can be executed
    #     at the same time. If it is -1, then all the files will be uploaded concurrently.
    #     The default can be set for this instance by passing "batch_size" in the
    #     constructor, or for all instances by setting the "gather_batch_size" key
    #     in ``fsspec.config.conf``, falling back to 1/8th of the system limit .
    #     """
    #     # from .implementations.local import LocalFileSystem, make_path_posix

    #     rpath = self._strip_protocol(rpath)
    #     if isinstance(lpath, str):
    #         lpath = make_path_posix(lpath)
    #     fs = LocalFileSystem()
    #     lpaths = fs.expand_path(lpath, recursive=recursive)
        
        
        
    #     rpaths = other_paths(
    #         lpaths, rpath, exists=isinstance(rpath, str) and await self._isdir(rpath)
    #     )

    #     is_dir = {l: os.path.isdir(l) for l in lpaths}
    #     rdirs = [r for l, r in zip(lpaths, rpaths) if is_dir[l]]
    #     file_pairs = [(l, r) for l, r in zip(lpaths, rpaths) if not is_dir[l]]

    #     await asyncio.gather(*[self._makedirs(d, exist_ok=True) for d in rdirs])
    #     batch_size = batch_size or self.batch_size

    #     coros = []
    #     callback.set_size(len(file_pairs))
    #     for lfile, rfile in file_pairs:
    #         callback.branch(lfile, rfile, kwargs)
    #         coros.append(self._put_file(lfile, rfile, **kwargs))

    #     return await _run_coros_in_chunks(
    #         coros, batch_size=batch_size, callback=callback
    #     )
    # put = sync_wrapper(_put)
    async def _put(self,
        lpath=None, 
        rpath=None,
        recursive=True,
        pin=True,
        chunker=262144, 
        return_json=True,
        return_cid = True,
        wrap_with_directory=False,
        **kwargs
    ):
        
        # if self.gateway_type == 'public': raise TypeError ('`put_file` and `put` functions require local/infura `gateway_type`')
        
        session = await self.set_session()

        if 'path' in kwargs:
            lpath = kwargs.pop('path')

        params = {}
        params['chunker'] = f'size-{chunker}'
        params['pin'] = 'true' if pin else 'false'
        params.update(kwargs)
        params['wrap-with-directory'] = 'true' if wrap_with_directory else 'false'

        assert os.path.exists(lpath), f'{lpath} does not exists'
        local_isdir = os.path.isdir(lpath)
        local_isfile = os.path.isfile(lpath)

        assert bool(local_isfile) != bool(local_isdir), \
                f'WTF, local_isfile: {local_isfile} && local_isdir: {local_isdir}'
        
        if local_isdir:
            assert bool(local_isfile) == False
            data, headers = stream_directory(lpath, chunk_size=chunker, recursive=recursive)
        else:
            assert bool(local_isfile) == True
            data, headers = stream_files(lpath, chunk_size=chunker)
        
        
        data = self.data_gen_wrapper(data=data)                             
        res = await self.gateway.api_post(endpoint='add', session=session, params=params, data=data, headers=headers)
        
        res =  await res.content.read()
        res = list(map(lambda x: json.loads(x), filter(lambda x: bool(x),  res.decode().split('\n'))))
        res = list(filter(lambda x: isinstance(x, dict) and x.get('Name'), res))
        res_hash = res[-1]["Hash"]
        
        if pin and not rpath:
            rpath='/'
        if rpath:
            rpath =  '/' + rpath.rstrip('/').lstrip('/') + '/'

            cid_hash = res[-1]["Hash"]
            ipfs_path = f'/ipfs/{cid_hash}'
            
            if  local_isdir:
                await self._cp(path1=f'/ipfs/{cid_hash}', path2=rpath )
            else:
                
                tmp_path = f'{rpath}/{cid_hash}'
                rdir = os.path.dirname(tmp_path)
                final_path = f'{rpath}/{os.path.basename(lpath)}'
                
                if not (await self._isdir(rdir)):
                    await self._mkdir(path=rdir)

                await self._cp(path1=ipfs_path, path2=rdir  )
                await self._cp(path1=tmp_path, path2=final_path )
                await self._rm_file(ipfs_path)
        
        
        if return_cid:
            return res_hash

        return res

    put = sync_wrapper(_put)


    async def _mkdir(self, path, create_parents=True, **kwargs):
        session = await self.set_session()
        if create_parents:
            rdir_list = path.lstrip('/').rstrip('/').split('/')
            current_r = '/'
            for r in rdir_list:
                current_r = os.path.join(current_r, r)
                if not (await self._isdir(current_r)):
                    await self._mkdir(path=current_r, create_parents=False)

        return await self.gateway.api_post(session=session, endpoint='files/mkdir', arg=path)


    async def _makedirs(self, path, exist_ok=False):
        if exist_ok:
            if all(await asyncio.gather(self._exists(path), self._isdir(path))):
                return path
        await self._mkdir(path, create_parents=True)
        # pass  # not necessary to implement, may not have directories




    async def _rm(self, path, recursive=False, batch_size=None, **kwargs):
        # TODO: implement on_error
        batch_size = batch_size or self.batch_size

        try:
            paths = await self._expand_path([path], recursive=recursive)
        except Exception:
            return []
        # paths = await self.filter_files(paths)
        return await _run_coros_in_chunks(
            [self._rm_file(p, **kwargs) for p in paths],
            batch_size=batch_size,
            nofiles=True,
        )

    async def _cat_file(self, path, start=None, end=None, **kwargs):
        path = self._strip_protocol(path)
        
        
        session = await self.set_session()
        return (await self.gateway.cat(session=session, path=path))[start:end]
    
    async def filter_files(self, paths):
        async def _file_filter(p):
            # FIX: returns path if file, else returns False
            if await self._isfile(p):
                return p
            else:
                return False
        paths = [_ for _ in await asyncio.gather(*[ _file_filter(p) for p in paths]) if bool(_) ]
        return paths

    async def _cat(
            self, path, recursive=False, on_error="raise", batch_size=None, **kwargs
        ):


            if await self._isdir(path=path):
                recursive = True
            
            paths = await self._expand_path(path, recursive=recursive)
            paths = await self.filter_files(paths)
            # print(paths, 'PATHS', path)
            coros = [self._cat_file(path, **kwargs) for path in paths]
            batch_size = batch_size or self.batch_size
            out = await _run_coros_in_chunks(
                coros, batch_size=batch_size, nofiles=True, return_exceptions=True
            )
            if on_error == "raise":
                ex = next(filter(is_exception, out), False)
                if ex:
                    raise ex
            
            assert len(paths) == len(out)
            if (
                len(out) >= 1
            ):
                if await self._isdir(path):
                    return {
                        k: v
                        for k, v in zip(paths, out)
                        if on_error != "omit" or not is_exception(v)
                    }
                else:
                    return out[0]

            else:
                raise Exception(f'WTF, out is not suppose to be <1 , len: {len(out)}')
    
    
    async def _info(self, path, **kwargs):
        path = self._strip_protocol(path)
        session = await self.set_session()
        info = await self.gateway.file_info(session=session, path=path)
        return info

    def open(self, path, mode="rb",  block_size="default",autocommit=True,
                cache_type="readahead", cache_options=None, size=None, **kwargs):
        

        return IPFSBufferedFile(
                            fs=self,
                            path=path,
                            mode=mode,
                            block_size=block_size,
                            autocommit=autocommit,
                            cache_type=cache_type,
                            cache_options=cache_options,
                            size=size
                        )

        # if mode == 'rb':
        #     data = self.cat_file(path)  # load whole chunk into memory
        #     return io.BytesIO(data)
        # elif mode == 'wb':
        #     self.put_file(path)
        # else:
        #     raise NotImplementedError

    def ukey(self, path):
        """returns the CID, which is by definition an unchanging identitifer"""
        return self.info(path)["CID"]

    # async def _get(self,
    #     rpath,
    #     lpath=None,
    #     **kwargs
    # ):
    #     if 'path' in kwargs:
    #         lpath = kwargs.pop('path')
        
    #     session = await self.set_session()
    #     if lpath is None: lpath = os.getcwd()
    #     self.full_structure = await self.gateway.get_links(session=session, rpath=rpath, lpath=lpath)
    #     await self.gateway.save_links(session=session, links=self.full_structure)
    # get=sync_wrapper(_get)

    async def _get_file(self, rpath, lpath, **kwargs):
        import shutil
        session = self._session
        # shutil.rmtree(lpath)
        data = await self._cat(path=rpath)
        f = open(lpath, mode='wb')
        f.write(data)
        f.close()

    async def _get(
        self, rpath, lpath, recursive=True, callback=_DEFAULT_CALLBACK, **kwargs
    ):
        """Copy file(s) to local.

        Copies a specific file or tree of files (if recursive=True). If lpath
        ends with a "/", it will be assumed to be a directory, and target files
        will go within. Can submit a list of paths, which may be glob-patterns
        and will be expanded.

        The get_file method will be called concurrently on a batch of files. The
        batch_size option can configure the amount of futures that can be executed
        at the same time. If it is -1, then all the files will be uploaded concurrently.
        The default can be set for this instance by passing "batch_size" in the
        constructor, or for all instances by setting the "gather_batch_size" key
        in ``fsspec.config.conf``, falling back to 1/8th of the system limit .
        """


        if len(lpath.split('.')) == 1 and len(lpath) > 1 and lpath[-1] != '/' :
            lpath += '/'

        rpath = self._strip_protocol(rpath)
        lpath = make_path_posix(lpath)

        root_dir_lpath = lpath if os.path.isdir(lpath) else os.path.dirname(lpath)

        rpaths = await self._expand_path(rpath, recursive=recursive)
        
        if len(lpath.split('.')) == 1:
            lpaths = other_paths(rpaths, lpath)
            [os.makedirs(os.path.dirname(lp), exist_ok=True) for lp in lpaths]
        else:
            lpaths = [lpath]

        batch_size = kwargs.pop("batch_size", self.batch_size)
        temp_rpaths, temp_lpaths = [], []

        for i,lp in enumerate(lpaths):
            if len(lp.split('.')) == 2:
                temp_rpaths.append(rpaths[i])
                temp_lpaths.append(lpaths[i])
            
            else:
                if await self._isfile(rpaths[i]):
                    temp_lpaths.append(os.path.join(lpaths[i]+''))
                    temp_rpaths.append(rpaths[i])

        rpaths = temp_rpaths
        lpaths = temp_lpaths
        # #TODO: not good for hidden files
        # lpaths = list(filter(lambda f: self.local_fs.isfile(f),lpaths))
        
        coros = []
        callback.set_size(len(lpaths))
        for lpath, rpath in zip(lpaths, rpaths):
            callback.branch(rpath, lpath, kwargs)
            coros.append(self._get_file(rpath, lpath, **kwargs))
        return await _run_coros_in_chunks(
            coros, batch_size=batch_size, callback=callback
        )


from fsspec.spec import  AbstractBufferedFile
import io
from fsspec.core import get_compression

class IPFSBufferedFile(io.IOBase):
    def __init__(
        self, path, mode, autocommit=True, fs=None, compression=None, **kwargs
    ):
        self.path = path
        self.mode = mode
        self.fs = fs
        self.f = None
        self.autocommit = autocommit
        self.compression = get_compression(path, compression)
        self.blocksize = io.DEFAULT_BUFFER_SIZE
        self._open()

    def _open(self):
        if self.f is None or self.f.closed:
            if self.autocommit or "w" not in self.mode:
                # self.temp = '/tmp'+self.path
                self.temp = self.path
                # os.makedirs(os.path.dirname(self.temp), exist_ok=True)
                self.f = open(self.temp, mode=self.mode)
                if self.compression:
                    compress = compr[self.compression]
                    self.f = compress(self.f, mode=self.mode)
            else:
                # TODO: check if path is writable?
                i, name = tempfile.mkstemp()
                os.close(i)  # we want normal open and normal buffered file
                self.temp = name
                self.f = open(name, mode=self.mode)
            if "w" not in self.mode:
                self.size = self.f.seek(0, 2)
                self.f.seek(0)
                self.f.size = self.size

    def _fetch_range(self, start, end):
        if self.__content is None:
            self.__content = self.fs.cat_file(self.path)
        content = self.__content[start:end]
        if "b" not in self.mode:
            return content.decode("utf-8")
        else:
            return content



    def __setstate__(self, state):
        self.f = None
        loc = state.pop("loc", None)
        self.__dict__.update(state)
        if "r" in state["mode"]:
            self.f = None
            self._open()
            self.f.seek(loc)

    def __getstate__(self):
        d = self.__dict__.copy()
        d.pop("f")
        if "r" in self.mode:
            d["loc"] = self.f.tell()
        else:
            if not self.f.closed:
                raise ValueError("Cannot serialise open write-mode local file")
        return d

    def commit(self):
        if self.autocommit:
            raise RuntimeError("Can only commit if not already set to autocommit")
        shutil.move(self.temp, self.path)

    def discard(self):
        # if self.autocommit:
        #     raise RuntimeError("Cannot discard if set to autocommit")
        os.remove(self.temp)

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return "r" not in self.mode

    def read(self, *args, **kwargs):
        return self.f.read(*args, **kwargs)

    def write(self, *args, **kwargs):
        return self.f.write(*args, **kwargs)

    def tell(self, *args, **kwargs):
        return self.f.tell(*args, **kwargs)

    def seek(self, *args, **kwargs):
        return self.f.seek(*args, **kwargs)

    def seekable(self, *args, **kwargs):
        return self.f.seekable(*args, **kwargs)

    def readline(self, *args, **kwargs):
        return self.f.readline(*args, **kwargs)

    def readlines(self, *args, **kwargs):
        return self.f.readlines(*args, **kwargs)

    def close(self):
        return self.f.close()

    @property
    def closed(self):
        
        return self.f.closed

    def __fspath__(self):
        # uniquely among fsspec implementations, this is a real, local path
        return self.path

    def __iter__(self):
        return self.f.__iter__()

    def __getattr__(self, item):
        return getattr(self.f, item)

    def __enter__(self):
        self._incontext = True
        return self



    def write_temp_to_ipfs(self):
        if 'w' in self.mode:
            self.cid = self.fs.put(lpath=self.temp, rpath=os.path.dirname(self.path), recursive=True)
            print(self.cid)
        # self.discard()
        

    def __exit__(self, exc_type, exc_value, traceback):
        
        self._incontext = False

        self.write_temp_to_ipfs()
        self.f.__exit__(exc_type, exc_value, traceback)





if __name__ == '__main__':

    import streamlit as st
    import fsspec
    from ipfsspec.asyn import AsyncIPFSFileSystem
    from fsspec import register_implementation
    from ipfsspec.utils import dict_equal, dict_hash
    import asyncio
    import io
    import os
    from datasets import load_dataset, Dataset
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    class fs:
        ipfs = AsyncIPFSFileSystem()
        local = fsspec.filesystem('file')

    # dataset = load_dataset("glue", "mrpc", split="train")
    # model = AutoModelForSequenceClassification.from_pretrained("bert-base-uncased")
    # tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")

    # fs.ipfs.rm('/hf/bro/yo')
    # st.write(deML.save_model(model=model, path='/hf/bro/yo'))

    # st.write(fs.ipfs.ls('/hf/bro/yo'))

    from transformers import AutoModel, AutoTokenizer
    from datasets import load_dataset, Dataset

    # dataset = load_dataset("glue", "mrpc", split="train")
    # model = AutoModel.from_pretrained("bert-base-uncased")
    tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")


    class deML:
        tmp_root_path = '/tmp/deML'
        fs = fs

        @staticmethod
        def get_tmp_path(path):
            tmp_path = os.path.join(deML.tmp_root_path, path)
            try:
                fs.local.mkdir(tmp_path, create_parents=True)
            except FileExistsError:
                pass
            
            return tmp_path
        
        
        @staticmethod
        def save_model(model, path:str):

            
            # fs.ipfs.mkdir(path, create_parents=True)
            
            tmp_path = deML.get_tmp_path(path=path)
            model.save_pretrained(tmp_path)
            fs.ipfs.mkdirs(path)
            
            trial_count = 0
            max_trials = 10
            while trial_count<max_trials:
                try:
                    cid= fs.ipfs.put(lpath=tmp_path, rpath=path, recursive=True)
                    break
                except fsspec.exceptions.FSTimeoutError:
                    trial_count += 1
                    print(f'Failed {trial_count}/{max_trials}')
                    
            
            fs.local.rm(tmp_path,  recursive=True)
            
            return cid
            

        @staticmethod
        def load_model(path):
            tmp_path = deML.get_tmp_path(path=path)
            fs.ipfs.get(lpath=tmp_path, rpath=path )
            model = AutoModel.from_pretrained(tmp_path)
            fs.local.rm(tmp_path,  recursive=True)
            
            return model

        @staticmethod
        def save_dataset(dataset, path:str):
            pass

        @staticmethod
        def load_dataset(path:str):
            
            pass
    print(fs.ipfs.rm('/hf/bert/tokenizer/'))
    cid = deML.save_model(model=tokenizer, path='/hf/bert/tokenizer')

    print(fs.ipfs.ls('/hf/bert/tokenizer/'))



    
    # def encode(examples):
    #     return tokenizer(examples["sentence1"], examples["sentence2"], truncation=True, padding="max_length")
    # dataset = dataset.map(encode, batched=True)
    # dataset = dataset.map(lambda examples: {"labels": examples["label"]}, batched=True)


    # dataset.set_format(type="torch", columns=["input_ids", "token_type_ids", "attention_mask", "labels"])
    # import torch
    # dataloader = torch.utils.data.DataLoader(dataset, batch_size=1)

    # model.save_pretrained(save_directory='/tmp')
    # with torch.no_grad():
    #     st.write(model(**next(iter(dataloader))))

