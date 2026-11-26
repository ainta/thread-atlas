"""A client of the existing local Codex App Server, using its Unix WebSocket."""
import asyncio
import json
from pathlib import Path

import aiohttp


async def output(*args, timeout=20, cwd=None):
    proc = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                               stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(f"{args[0]} exited with status {proc.returncode}")
    return stdout


class Codex:
    def __init__(self, socket=None):
        self.socket = socket
        self.seq = 0
        self.pending = {}
        self.http = self.ws = self.reader = None

    async def __aenter__(self):
        try:
            socket = self.socket or json.loads(await output("codex", "app-server", "daemon", "version"))["socketPath"]
            if not Path(socket).exists():
                raise RuntimeError("Codex App Server is offline")
            self.http = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=socket))
            self.ws = await self.http.ws_connect("http://localhost/", max_msg_size=96*1024*1024)
            self.reader = asyncio.create_task(self.listen())
            await self.call("initialize", {"clientInfo":{"name":"thread_atlas","version":"0.1.0"},
                                           "capabilities":{"experimentalApi": True}})
            await self.ws.send_json({"method":"initialized"})
            return self
        except BaseException:
            await self.__aexit__(None,None,None)
            raise

    async def __aexit__(self, *args):
        if self.ws:
            await self.ws.close()
        if self.reader:
            await self.reader
        if self.http:
            await self.http.close()

    async def listen(self):
        try:
            async for msg in self.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if "method" in data and "id" in data:
                    # Leave approval decisions to the originating client; never auto-approve tools.
                    await self.ws.send_json({"id":data["id"],"error":{"code":-32601,"message":"Approval UI unavailable in Thread Atlas"}})
                    continue
                f = self.pending.get(data.get("id"))
                if f and not f.done():
                    if "error" in data:
                        f.set_exception(RuntimeError(data["error"].get("message","Codex request failed")))
                    else:
                        f.set_result(data.get("result"))
        finally:
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(RuntimeError("Codex connection closed"))

    async def call(self, method, params):
        self.seq += 1
        key = self.seq
        f = self.pending[key] = asyncio.get_running_loop().create_future()
        try:
            await self.ws.send_json({"id":key,"method":method,"params":params})
            return await asyncio.wait_for(f, 35)
        finally:
            self.pending.pop(key,None)

    async def send(self, sid, prompt):
        thread = (await self.call("thread/read", {"threadId":sid}))["thread"]
        if thread["status"]["type"] in ("active","systemError"):
            raise RuntimeError("Thread is busy or unavailable")
        resumed = (await self.call("thread/resume", {"threadId":sid,"excludeTurns":True}))["thread"]
        if resumed["id"] != sid or resumed["status"]["type"]=="active":
            raise RuntimeError("Original thread is unavailable")
        return (await self.call("turn/start", {"threadId":sid,"input":[{"type":"text","text":prompt}]}))["turn"]["id"]
