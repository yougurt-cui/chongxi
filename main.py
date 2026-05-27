"""Flask application factory for external APIs."""

from __future__ import annotations

from flask import Flask, jsonify

from app.api.consumer_api import consumer_api
from app.api.exception_api import exception_api
from app.api.pipeline_api import pipeline_api
from app.api.process_signal_api import process_signal_api


def create_app() -> Flask:
    flask_app = Flask(__name__)
    flask_app.register_blueprint(consumer_api)
    flask_app.register_blueprint(exception_api)
    flask_app.register_blueprint(pipeline_api)
    flask_app.register_blueprint(process_signal_api)

    @flask_app.get("/health")
    def health() -> tuple[dict, int]:
        return {"status": "ok"}, 200

    return flask_app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
