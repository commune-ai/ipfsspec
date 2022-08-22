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
                # self.size = self.f.seek(0, 2)
                # self.f.seek(0)
                # self.f.size = self.size
                pass

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



    def flush_to_ipfs(self):
        if 'w' in self.mode:
            self.cid = self.fs.put(lpath=self.temp, rpath=os.path.dirname(self.path), recursive=True)
            print(self.cid)
        # self.discard()
        

    def __exit__(self, exc_type, exc_value, traceback):
        
        self._incontext = False

        self.flush_to_ipfs()
        self.f.__exit__(exc_type, exc_value, traceback)

