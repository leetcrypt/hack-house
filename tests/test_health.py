class TestHealth:

    def test_health_ok(self, test_client):
        _, response = test_client.get("/health")

        assert response.status == 200
        data = response.json
        assert data["status"] == "ok"
        assert "messages" in data
        assert "users" in data
        assert "timestamp" in data
