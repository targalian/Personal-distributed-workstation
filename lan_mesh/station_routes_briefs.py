"""Project description editing, generation and portable Markdown export."""
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .project_brief import ProjectBrief, RevisionConflict
from .station_routes_common import check_secretary


class SaveBrief(BaseModel):
    document: ProjectBrief
    expected_revision: int = Field(ge=0)


class GenerateBrief(BaseModel):
    expected_revision: int = Field(ge=0)


class ReviewBrief(BaseModel):
    proposal_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=0)
    selected: list[str] = Field(default_factory=list, max_length=2048)


def build_brief_routes(controller):
    router = APIRouter(prefix="/api/conversations/{conv_id}/brief")

    def handler(conv_id):
        check_secretary(controller)
        chat = getattr(controller, "chat_handler", None)
        if not chat:
            raise HTTPException(503, "聊天处理器未初始化")
        if not chat._get_conv_meta(conv_id):
            raise HTTPException(404, "对话不存在")
        return chat

    @router.get("")
    def get_brief(conv_id: str):
        return handler(conv_id).briefs.get(conv_id)

    @router.put("")
    def save_brief(conv_id: str, payload: SaveBrief):
        try:
            return handler(conv_id).briefs.save(conv_id, payload.document.model_dump(), payload.expected_revision)
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/generate")
    def generate_brief(conv_id: str, payload: GenerateBrief):
        try:
            return handler(conv_id).generate_project_brief(conv_id, payload.expected_revision)
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/markdown")
    def export_brief(conv_id: str):
        value = handler(conv_id).briefs.get(conv_id)
        return Response(value["markdown"], media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote("项目描述.md"),
        })

    @router.post("/review")
    def review_brief(conv_id: str, payload: ReviewBrief):
        try:
            return handler(conv_id).briefs.review(
                conv_id, payload.proposal_id, payload.expected_revision, payload.selected)
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return router
