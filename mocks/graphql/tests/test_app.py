from graphql_mock.app import app


def test_healthz():
    client = app.test_client()
    assert client.get("/healthz").json == {"status": "ok"}


def test_introspection_returns_the_schema():
    client = app.test_client()
    resp = client.post("/graphql", json={"query": "query { __schema { queryType { name } } }"})
    assert resp.json["data"]["__schema"]["queryType"]["name"] == "Query"


def test_user_query_returns_a_known_user():
    client = app.test_client()
    resp = client.post(
        "/graphql",
        json={"query": "query GetUser($id: ID!) { user(id: $id) { id name } }", "variables": {"id": "u1"}},
    )
    assert resp.json["data"]["user"]["name"] == "Ada"


def test_user_query_for_an_unknown_id_returns_errors():
    client = app.test_client()
    resp = client.post(
        "/graphql",
        json={"query": "query GetUser($id: ID!) { user(id: $id) { id } }", "variables": {"id": "nope"}},
    )
    assert resp.json["data"] is None
    assert resp.json["errors"]


def test_create_user_mutation_adds_a_user():
    client = app.test_client()
    resp = client.post(
        "/graphql",
        json={
            "query": "mutation CreateUser($name: String!) { createUser(name: $name) { id name } }",
            "variables": {"name": "Grace"},
        },
    )
    assert resp.json["data"]["createUser"]["name"] == "Grace"
