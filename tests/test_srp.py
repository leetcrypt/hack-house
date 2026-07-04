import base64
import srp


class TestSRPFlow:
    def test_srp_init_success(self, test_client):
        usr = srp.User(b"chat", b"testpassword")
        _, A = usr.start_authentication()

        _, response = test_client.post(
            "/srp/init",
            json={
                "username": "testuser",
                "A": base64.b64encode(A).decode(),
            },
        )

        assert response.status == 200
        data = response.json
        assert "user_id" in data
        assert "B" in data
        assert "salt" in data

    def test_srp_init_missing_a(self, test_client):
        _, response = test_client.post(
            "/srp/init",
            json={"username": "testuser"},
        )

        assert response.status == 400

    def test_srp_verify_invalid_session(self, test_client):
        _, response = test_client.post(
            "/srp/verify",
            json={
                "user_id": "nonexistent",
                "username": "testuser",
                "M": base64.b64encode(b"fake").decode(),
            },
        )
        assert response.status == 401
