import asyncio
import weakref

import aiohttp

from fsspec.asyn import AsyncFileSystem, sync, sync_wrapper

class AsyncIPFSGateway:
    # stats
    # roundtrip time
    # troughput

    resolution = "path"

    def __init__(self, url):
        self.url = url

    async def api_get(self, endpoint, session, **kwargs):
        return await session.get(self.url + "/api/v0/" + endpoint, params=kwargs, trace_request_ctx={'gateway': self.url})

    async def api_post(self, endpoint, session, **kwargs):
        return await session.post(self.url + "/api/v0/" + endpoint, params=kwargs, trace_request_ctx={'gateway': self.url})

    async def version(self, session):
        res = await self.api_get("version", session)
        res.raise_for_status()
        return await res.json()

    async def stat(self, path, session):
        res = await self.api_get("files/stat", session, arg=path)
        res.raise_for_status()
        return await res.json()

    async def file_info(self, path, session):
        info = {"name": path}

        headers = {"Accept-Encoding": "identity"}  # this ensures correct file size
        if self.resolution == "path":
            res = await session.head(self.url + "/ipfs/" + path, trace_request_ctx={'gateway': self.url}, headers=headers)
            #print(res)
        elif self.resolution == "subdomain":
            raise NotImplementedError("subdomain resolution is not yet implemented")
        else:
            raise NotImplementedError(f"'{self.resolution}' resolution is not known")

        async with res:
            self._raise_not_found_for_status(res, path)
            if "Content-Length" in res.headers:
                info["size"] = int(res.headers["Content-Length"])
            elif "Content-Range" in res.headers:
                info["size"] = int(res.headers["Content-Range"].split("/")[1])

            if "ETag" in res.headers:
                etag = res.headers["ETag"].strip("\"")
                info["ETag"] = etag
                if etag.startswith("DirIndex"):
                    info["type"] = "directory"
                    info["CID"] = etag.split("-")[-1]
                else:
                    info["type"] = "file"
                    info["CID"] = etag

        return info

    async def cat(self, path, session):
        info = {"name": path}

        if self.resolution == "path":
            res = await session.get(self.url + "/ipfs/" + path, trace_request_ctx={'gateway': self.url})
        elif self.resolution == "subdomain":
            raise NotImplementedError("subdomain resolution is not yet implemented")
        else:
            raise NotImplementedError(f"'{self.resolution}' resolution is not known")

        async with res:
            self._raise_not_found_for_status(res, path)
            return await res.read()

    async def ls(self, path, session):
        res = await self.api_get("ls", session, arg=path)
        self._raise_not_found_for_status(res, path)
        resdata = await res.json()
        types = {1: "directory", 2: "file"}
        return [{
                    "name": path + "/" + link["Name"],
                    "CID": link["Hash"],
                    "type": types[link["Type"]],
                    "size": link["Size"],
                }
                for link in resdata["Objects"][0]["Links"]]

    def _raise_not_found_for_status(self, response, url):
        """
        Raises FileNotFoundError for 404s, otherwise uses raise_for_status.
        """
        if response.status == 404:
            raise FileNotFoundError(url)
        response.raise_for_status()

# required functions
# cat
# ls
# info

async def get_client(**kwargs):
    return aiohttp.ClientSession(**kwargs)

class AsyncIPFSFileSystem(AsyncFileSystem):
    sep = "/"
    protocol = "aipfs"

    def __init__(self, asynchronous=False, loop=None, client_kwargs=None, **storage_options):
        super().__init__(self, asynchronous=asynchronous, loop=loop, **storage_options)
        self._session = None

        self.client_kwargs = client_kwargs or {}
        self.get_client = get_client

        self.gateway = AsyncIPFSGateway("http://127.0.0.1:8080")

        if not asynchronous:
            sync(self.loop, self.set_session)

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

    async def set_session(self):
        if self._session is None:
            self._session = await self.get_client(loop=self.loop, **self.client_kwargs)
            if not self.asynchronous:
                weakref.finalize(self, self.close_session, self.loop, self._session)
        return self._session

    async def _ls(self, path, detail=True, **kwargs):
        path = self._strip_protocol(path)
        session = await self.set_session()
        res = await self.gateway.ls(path, session)
        if detail:
            return res
        else:
            return [r["name"] for r in res]

    ls = sync_wrapper(_ls)

    async def _cat_file(self, path, start=None, end=None, **kwargs):
        path = self._strip_protocol(path)
        session = await self.set_session()
        return (await self.gateway.cat(path, session))[start:end]

    async def _info(self, path, **kwargs):
        path = self._strip_protocol(path)
        session = await self.set_session()
        return await self.gateway.file_info(path, session)


class GatewayTracer:
    def __init__(self):
        from collections import defaultdict
        self.samples = defaultdict(list)

    def make_trace_config(self):
        trace_config = aiohttp.TraceConfig()
        trace_config.on_request_start.append(self.on_request_start)
        trace_config.on_request_end.append(self.on_request_end)
        return trace_config
        
    async def on_request_start(self, session, trace_config_ctx, params):
        trace_config_ctx.start = asyncio.get_event_loop().time()
        #print("Starting request")

    async def on_request_end(self, session, trace_config_ctx, params):
        #print(trace_config_ctx)
        trace_config_ctx.end = asyncio.get_event_loop().time()
        elapsed = trace_config_ctx.end - trace_config_ctx.start
        status = params.response.status
        gateway = trace_config_ctx.trace_request_ctx.get("gateway", None)
        self.samples[gateway].append({"url": params.url, "method": params.method, "elapsed": elapsed, "status": status})
        #print("Ending request")