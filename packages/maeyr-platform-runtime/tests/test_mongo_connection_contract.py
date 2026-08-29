from __future__ import annotations

import pytest

from maeyr_platform.mongo import mongo_connection_uri, redact_mongo_uri


def test_complete_uri_is_authoritative_and_preserves_atlas_options() -> None:
    uri = (
        "mongodb+srv://user:p%40ss@cluster.mongodb.net/app"
        "?retryWrites=true&w=majority&authSource=admin"
    )

    assert mongo_connection_uri({"MONGODB_URI": uri}) == uri


def test_decomposed_contract_escapes_credentials_and_supports_replica_sets() -> None:
    resolved = mongo_connection_uri(
        {
            "MONGO_HOST": "mongo-0.mongo:27017,mongo-1.mongo:27017",
            "MONGO_USERNAME": "service@example.com",
            "MONGO_PASSWORD": "p@ss/word",
            "MONGO_REPLICA_SET": "rs platform",
            "MONGO_AUTH_SOURCE": "admin",
        }
    )

    assert resolved.startswith(
        "mongodb://service%40example.com:p%40ss%2Fword@"
        "mongo-0.mongo:27017,mongo-1.mongo:27017/"
    )
    assert "replicaSet=rs+platform" in resolved
    assert "authSource=admin" in resolved


def test_srv_and_mounted_tls_files_are_projected_without_rewriting_uri() -> None:
    resolved = mongo_connection_uri(
        {
            "MONGODB_URI": "mongodb+srv://cluster.mongodb.net/application?appName=api",
            "MONGO_TLS_CA_FILE": "/var/run/secrets/mongo/ca.pem",
            "MONGO_TLS_CERT_FILE": "/var/run/secrets/mongo/client.pem",
        }
    )

    assert resolved.startswith(
        "mongodb+srv://cluster.mongodb.net/application?appName=api&tls=true"
    )
    assert "tlsCAFile=%2Fvar%2Frun%2Fsecrets%2Fmongo%2Fca.pem" in resolved
    assert "tlsCertificateKeyFile=%2Fvar%2Frun%2Fsecrets%2Fmongo%2Fclient.pem" in resolved


def test_existing_tls_uri_options_are_not_duplicated() -> None:
    resolved = mongo_connection_uri(
        {
            "MONGODB_URI": "mongodb://mongo:27017/?tls=true&tlsCAFile=%2Furi-ca.pem",
            "MONGO_TLS_ENABLED": "true",
            "MONGO_TLS_CA_FILE": "/environment-ca.pem",
        }
    )

    assert resolved.count("tls=") == 1
    assert resolved.count("tlsCAFile=") == 1
    assert "%2Furi-ca.pem" in resolved


@pytest.mark.parametrize(
    "environment, message",
    [
        ({}, "MONGODB_URI or MONGO_HOST"),
        ({"MONGODB_URI": "https://mongo.example"}, "complete mongodb"),
        (
            {"MONGO_HOST": "mongo", "MONGO_USERNAME": "user"},
            "must either both be set",
        ),
        (
            {
                "MONGO_HOST": "mongo",
                "MONGO_TLS_CERT_FILE": "/client.crt",
                "MONGO_TLS_KEY_FILE": "/client.key",
            },
            "combined certificate/key PEM",
        ),
        (
            {
                "MONGO_HOST": "mongo",
                "MONGO_TLS_ALLOW_INVALID_CERTIFICATES": "true",
                "APP_ENVIRONMENT": "production",
            },
            "forbidden in production",
        ),
    ],
)
def test_invalid_contracts_fail_without_echoing_secrets(
    environment: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message) as exc_info:
        mongo_connection_uri(environment)

    assert environment.get("MONGO_PASSWORD", "not-present") not in str(exc_info.value)


def test_redaction_hides_credentials_and_secret_query_values() -> None:
    redacted = redact_mongo_uri(
        "mongodb://user:password@mongo:27017/app?authToken=top-secret"
        "&authMechanismProperties=AWS_SESSION_TOKEN:also-secret&appName=worker"
    )

    assert "user" not in redacted
    assert "password" not in redacted
    assert "top-secret" not in redacted
    assert "also-secret" not in redacted
    assert "credentials-redacted" in redacted
    assert "authToken=%3Credacted%3E" in redacted
    assert "appName=worker" in redacted
