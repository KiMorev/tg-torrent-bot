import unittest

from kinopoisk import KinopoiskClient


class FakeResponse:
    def __init__(self, json_data) -> None:
        self._json_data = json_data

    def raise_for_status(self) -> None:
        return None

    def json(self):
        if isinstance(self._json_data, BaseException):
            raise self._json_data
        return self._json_data


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.headers = {}

    def get(self, *args, **kwargs) -> FakeResponse:
        return self.response


class KinopoiskClientTests(unittest.TestCase):
    def test_get_director_ignores_malformed_staff_response(self) -> None:
        client = KinopoiskClient("secret")
        client._session = FakeSession(FakeResponse({"bad": "shape"}))

        self.assertEqual(client._get_director(123), "")

    def test_get_director_skips_malformed_people(self) -> None:
        client = KinopoiskClient("secret")
        client._session = FakeSession(FakeResponse([
            "bad",
            {"professionKey": "ACTOR", "nameRu": "Actor"},
            {"professionKey": "DIRECTOR", "nameRu": "Director One"},
            {"professionKey": "DIRECTOR", "nameEn": "Director Two"},
            {"professionKey": "DIRECTOR", "nameRu": "Director Three"},
        ]))

        self.assertEqual(client._get_director(123), "Director One, Director Two")


if __name__ == "__main__":
    unittest.main()
