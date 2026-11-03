# Habits
Habit tracker in the style of the Productive app: week strip, daily progress card, habits grouped by time of day, streaks, skip days, month calendar per habit, stats tab. FastAPI + Postgres, one page `app/static/index.html`. Live: https://habits.romijn.app (everything private, login with the edit password).
Deploy: push to `main`; `deploy.sh` (user timer `habits-deploy`) rsyncs the pushed commit to the Docker host, health-checks it and rolls back on failure.
