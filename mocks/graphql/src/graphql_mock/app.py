"""In-memory mock GraphQL API: hand-resolved, no GraphQL library.

Only needs to answer the standard introspection query and two operations
(`user(id)` query, `createUser(name)` mutation) — a full GraphQL engine is
more than this mock's job requires.
"""

from flask import Flask, jsonify, request

app = Flask(__name__)

_USERS: dict[str, dict[str, object]] = {
    "u1": {"id": "u1", "name": "Ada", "ssn": "000-00-0000"},
}
_next_id = 2

_SCHEMA = {
    "queryType": {"name": "Query"},
    "mutationType": {"name": "Mutation"},
    "types": [
        {
            "kind": "OBJECT",
            "name": "Query",
            "fields": [
                {
                    "name": "user",
                    "args": [
                        {
                            "name": "id",
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None},
                            },
                        }
                    ],
                    "type": {"kind": "OBJECT", "name": "User", "ofType": None},
                }
            ],
        },
        {
            "kind": "OBJECT",
            "name": "Mutation",
            "fields": [
                {
                    "name": "createUser",
                    "args": [
                        {
                            "name": "name",
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {"kind": "SCALAR", "name": "String", "ofType": None},
                            },
                        }
                    ],
                    "type": {"kind": "OBJECT", "name": "User", "ofType": None},
                }
            ],
        },
        {
            "kind": "OBJECT",
            "name": "User",
            "fields": [
                {"name": "id", "args": [], "type": {"kind": "SCALAR", "name": "ID", "ofType": None}},
                {"name": "name", "args": [], "type": {"kind": "SCALAR", "name": "String", "ofType": None}},
                {"name": "ssn", "args": [], "type": {"kind": "SCALAR", "name": "String", "ofType": None}},
            ],
        },
    ],
}


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.post("/graphql")
def graphql():
    payload = request.get_json(force=True, silent=True) or {}
    query = payload.get("query", "")
    variables = payload.get("variables") or {}

    if "__schema" in query:
        return jsonify(data={"__schema": _SCHEMA})

    if "createUser" in query:
        global _next_id
        name = variables.get("name")
        if not name:
            return jsonify(data=None, errors=[{"message": "name is required"}])
        user_id = f"u{_next_id}"
        _next_id += 1
        user = {"id": user_id, "name": name, "ssn": "000-00-0000"}
        _USERS[user_id] = user
        return jsonify(data={"createUser": {"id": user["id"], "name": user["name"]}})

    if "user" in query:
        user_id = variables.get("id")
        user = _USERS.get(user_id)
        if user is None:
            return jsonify(data=None, errors=[{"message": f"unknown user {user_id!r}"}])
        return jsonify(data={"user": {"id": user["id"], "name": user["name"]}})

    return jsonify(data=None, errors=[{"message": "unrecognized operation"}]), 400
