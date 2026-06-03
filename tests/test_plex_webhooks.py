import asyncio
import json
import unittest

from plex_webhooks import PlexWebhookServer, PlexWebhookState, parse_plex_payload


class FakeRequest:
    def __init__(self, *, token="tok", payload=None, content_type="application/json"):
        self.query = {"token": token} if token else {}
        self.headers = {}
        self.content_type = content_type
        self._payload = payload or {}

    async def json(self):
        return self._payload

    async def post(self):
        return {"payload": json.dumps(self._payload)}


class FakePart:
    name = "payload"

    def __init__(self, payload):
        self.payload = payload

    async def text(self):
        return json.dumps(self.payload)

    async def release(self):
        return None


class FakeMultipartRequest(FakeRequest):
    def __init__(self, payload):
        super().__init__(payload=payload, content_type="multipart/form-data")

    async def multipart(self):
        part = FakePart(self._payload)

        class Reader:
            def __init__(self):
                self.done = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.done:
                    raise StopAsyncIteration
                self.done = True
                return part

        return Reader()


class PlexWebhookPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_json_payload(self):
        request = FakeRequest(payload={"event": "library.new"})

        payload = await parse_plex_payload(request)

        self.assertEqual(payload["event"], "library.new")

    async def test_parses_multipart_payload_field(self):
        request = FakeMultipartRequest({"event": "library.new"})

        payload = await parse_plex_payload(request)

        self.assertEqual(payload["event"], "library.new")


class PlexWebhookServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_requires_token(self):
        state = PlexWebhookState()
        server = PlexWebhookServer(
            host="127.0.0.1",
            port=8099,
            token="secret",
            debounce_seconds=10,
            trigger=lambda _event: asyncio.sleep(0),
            state=state,
        )

        response = await server.handle_health(FakeRequest(token="bad"))

        self.assertEqual(response.status, 401)
        self.assertEqual(state.invalid_token_count, 1)

    async def test_webhook_triggers_once_and_debounces_second_event(self):
        calls = []

        async def trigger(event):
            calls.append(event)

        state = PlexWebhookState()
        server = PlexWebhookServer(
            host="127.0.0.1",
            port=8099,
            token="secret",
            debounce_seconds=60,
            trigger=trigger,
            state=state,
        )
        payload = {
            "event": "library.new",
            "Metadata": {"type": "movie", "title": "Dune", "ratingKey": "42"},
        }

        first = await server.handle_webhook(FakeRequest(token="secret", payload=payload))
        second = await server.handle_webhook(FakeRequest(token="secret", payload=payload))
        await asyncio.sleep(0)

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["title"], "Dune")
        self.assertEqual(state.trigger_count, 1)
        self.assertEqual(state.debounced_count, 1)
