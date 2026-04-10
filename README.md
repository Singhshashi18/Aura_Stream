# Aura-Stream

Realtime voice agent stack with Django Channels backend and React frontend.

## What is implemented

1. WebSocket-first audio stream (`/ws/call/`) with 16k mono PCM chunks
2. Server-side VAD gate (RMS-based) to separate speech vs silence chunks
3. Barge-in signal when user speech arrives during generation
4. Agent loop with tool calling and DB logging
5. Thought extraction from `<|think|>...</|think|>` into `ThoughtLog`
6. React frontend for Start/Stop call + live stream logs

## Database models

1. `AuraSession`
2. `ThoughtLog`
3. `AudioArtifact`
4. `AgentActivity`

## Tool functions

1. `check_inventory(item_id)` -> reads the app inventory map
2. `update_user_mood(sentiment_score)` -> logs activity result

## Setup

1. Install backend deps:
	`pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill values
3. Run migrations:
	`python manage.py makemigrations`
	`python manage.py migrate`
4. Run backend:
	`python manage.py runserver`
5. Run frontend:
	`cd frontend`
	`npm install`
	`npm run dev`
6. Open frontend:
	`http://127.0.0.1:5173`

## Runtime flow

1. Frontend connects to `ws://127.0.0.1:8001/ws/call/`
2. Audio chunks stream continuously
3. Backend reports `buffer_update` + VAD stats
4. On stop, backend runs agent turn with prompt
5. Tool calls are executed, stored in `AgentActivity`
6. Thought/final response are stored in `ThoughtLog`

## Environment variables

1. `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`
2. `OPENAI_API_KEY`
3. `OPENAI_BASE_URL` (default `https://api.openai.com/v1`)
4. `OPENAI_MODEL` (realtime session metadata model)
5. `OPENAI_AGENT_MODEL` (tool-calling model, recommended `gpt-4o-mini`)
