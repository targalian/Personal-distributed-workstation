"""Project description editing, portable Markdown export and confirmed handoff."""
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .brief_handoff import BriefHandoff
from .brief_readiness import brief_readiness
from .brief_review import brief_changes
from .project_brief import BriefStore, ProjectBrief, RevisionConflict
from .station_routes_common import check_secretary


class SaveBrief(BaseModel):
    document: ProjectBrief
    expected_revision: int = Field(ge=0)


class ReviewBrief(BaseModel):
    proposal_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=0)
    selected: list[str] = Field(default_factory=list, max_length=2048)


class RestoreBrief(BaseModel):
    expected_revision: int = Field(ge=0)


def build_brief_routes(controller):
    """Serve the saved project description of one conversation.

    The store lives beside the conversation files, so this needs no new table.
    Generation endpoints arrive with the chat integration; this router only
    edits, exports, reviews and dispatches what is already saved.
    """
    router = APIRouter(prefix="/api/conversations/{conv_id}/brief")

    def handler(conv_id):
        check_secretary(controller)
        chat = getattr(controller, "chat_handler", None)
        if not chat:
            raise HTTPException(503, "聊天处理器未初始化")
        if not chat._get_conv_meta(conv_id):
            raise HTTPException(404, "对话不存在")
        store = BriefStore(chat._get_conv_dir())
        return store, BriefHandoff(store, controller)

    @router.get("")
    def get_brief(conv_id: str):
        store, _ = handler(conv_id)
        return store.get(conv_id)

    @router.put("")
    def save_brief(conv_id: str, payload: SaveBrief):
        store, _ = handler(conv_id)
        try:
            return store.save(conv_id, payload.document.model_dump(), payload.expected_revision)
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/markdown")
    def export_brief(conv_id: str):
        store, _ = handler(conv_id)
        value = store.get(conv_id)
        return Response(value["markdown"], media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote("项目描述.md"),
        })

    @router.post("/review")
    def review_brief(conv_id: str, payload: ReviewBrief):
        store, _ = handler(conv_id)
        try:
            return store.review(conv_id, payload.proposal_id, payload.expected_revision,
                                payload.selected)
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/history")
    def list_history(conv_id: str, before_revision: int | None = Query(default=None, ge=1)):
        store, _ = handler(conv_id)
        return store.history(conv_id, before_revision)

    @router.get("/history/{revision}")
    def get_revision(conv_id: str, revision: int):
        store, _ = handler(conv_id)
        try:
            with store.lock:
                historical = store.revision(conv_id, revision)
                current = store.get(conv_id)
                return {**historical, "current_revision": current["revision"],
                        "changes": brief_changes(current["document"], historical["document"])}
        except KeyError as exc:
            raise HTTPException(404, "历史版本不存在") from exc

    @router.post("/history/{revision}/restore")
    def restore_revision(conv_id: str, revision: int, payload: RestoreBrief):
        store, _ = handler(conv_id)
        try:
            return store.restore(conv_id, revision, payload.expected_revision)
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "历史版本不存在") from exc

    @router.get("/readiness")
    def get_readiness(conv_id: str):
        store, _ = handler(conv_id)
        return brief_readiness(store.get(conv_id))

    @router.get("/handoffs")
    def list_handoffs(conv_id: str):
        _, handoffs = handler(conv_id)
        return {"handoffs": handoffs.list(conv_id)}

    @router.get("/handoffs/{revision}")
    def get_handoff(conv_id: str, revision: int):
        _, handoffs = handler(conv_id)
        try:
            return handoffs.get(conv_id, revision)
        except KeyError as exc:
            raise HTTPException(404, "下发记录不存在") from exc

    @router.post("/handoffs")
    def dispatch_brief(conv_id: str, payload: RestoreBrief):
        _, handoffs = handler(conv_id)
        try:
            return handoffs.dispatch(conv_id, payload.expected_revision)
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return router
