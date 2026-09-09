"""Historical V2 integration harness, explicitly separate from V3 product tests.

The retained Agent class supplies shared feedback/repair helpers. Its historical
contracts still have regression value, but its old planner and Auto gate behavior
must not be confused with the application's new default graph. No production
switch or checkpoint migration is introduced by this harness.
"""
from contextlib import asynccontextmanager

from tcg.agent import Agent
from tcg.main import create_app as current_app


def create_app(*args, **kwargs):
    app = current_app(*args, **kwargs)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def historical_lifespan(application):
        async with original_lifespan(application):
            engine, store = app.state.engine, app.state.store
            engine.agent = Agent(engine, engine.agent.graph.checkpointer)
            original_create = store.create_run

            def historical_run(chat_id, request):
                message, run = original_create(chat_id, request)
                if request.get('experience') == 'agent':
                    run = store.update_run(run['id'], graph_version=2)
                return message, run

            store.create_run = historical_run
            yield

    app.router.lifespan_context = historical_lifespan
    return app
