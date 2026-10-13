import asyncio
import unittest

import idle_load


class IdleSockets(unittest.IsolatedAsyncioTestCase):
    async def test_connections_are_held_without_payload_and_detect_eof(self):
        writers = []

        async def accept(reader, writer):
            writers.append(writer)

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        sockets = []
        try:
            port = server.sockets[0].getsockname()[1]
            sockets, errors = await idle_load.open_idle("127.0.0.1", port, 32, 8, 1)
            self.assertEqual(errors, [])
            self.assertEqual(len(sockets), 32)
            self.assertTrue(all(idle_load.alive(sock) for sock in sockets))
            for writer in writers:
                writer.close()
                await writer.wait_closed()
            # EOF delivery is asynchronous; bound the wait instead of relying
            # on scheduler timing or treating a graceful close as still alive.
            async def wait_for_eof():
                while any(idle_load.alive(sock) for sock in sockets):
                    await asyncio.sleep(.001)
            await asyncio.wait_for(wait_for_eof(), 1)
        finally:
            for sock in sockets:
                sock.close()
            server.close()
            await server.wait_closed()

    async def test_refused_connections_are_reported(self):
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        sockets, errors = await idle_load.open_idle("127.0.0.1", port, 4, 2, 1)
        self.assertEqual(sockets, [])
        self.assertEqual(len(errors), 4)
