class TestWebSocket:

    def test_ws_connect_no_user_id(self, test_client):
        _, ws = test_client.websocket("/ws/chat")
        assert ws is not None

    def test_ws_connect_invalid_session(self, test_client):

        _, ws = test_client.websocket("/ws/chat?user_id=invalid123")
        assert ws is not None
