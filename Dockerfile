FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install \
    --no-cache-dir \
    --disable-pip-version-check \
    --root-user-action=ignore \
    --index-url https://pypi.org/simple \
    -r requirements.txt

COPY access_control.py app_context.py bot.py config.py diagnostics.py download_station.py state_store.py storage.py subscription_policy.py series_bulk_planner.py series_continue.py search_intent.py voice_transcription.py gpt_client.py gpt_features.py rutracker.py kinopoisk.py jackett.py jackett_subscriptions.py movie_discovery.py formatters.py keyboards.py plex.py progressive_status.py task_policies.py task_views.py torrent_utils.py tracker_service.py tmdb.py tvmaze.py ./
COPY assets/ ./assets/

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, sys; sys.exit(0 if os.path.exists('/proc/1/status') else 1)"

CMD ["python", "/app/bot.py"]
