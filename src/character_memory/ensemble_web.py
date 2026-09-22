from __future__ import annotations

from pydantic import BaseModel, Field

from character_memory.ensemble_builder import EnsembleBuilderService, EnsembleRepository


class EnsembleStartRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)


class EnsembleConfirmRequest(BaseModel):
    selected_indices: list[int] = Field(min_length=2, max_length=12)
    confirm_over_soft_limit: bool = False


def attach_ensemble_routes(app):
    """One prompt -> research -> one confirmation -> normal Character + Group."""

    from fastapi import HTTPException

    access = getattr(app.state, "character_memory", None)
    if access is None:
        raise RuntimeError("create_api() must expose app.state.character_memory before ensemble routes attach")

    repository = EnsembleRepository(access.read_store)
    service = EnsembleBuilderService(access, repository)
    access.ensemble_repository = repository
    access.ensemble_service = service

    @app.post("/v1/ensembles")
    def start_ensemble(req: EnsembleStartRequest):
        try:
            return {"build": service.start(req.prompt)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"群聊创建失败：{exc}") from exc

    @app.get("/v1/ensembles/{group_id}")
    def get_ensemble(group_id: str):
        build = repository.get(group_id)
        if build is None:
            raise HTTPException(status_code=404, detail="ensemble build not found")
        return {"build": service.payload(build)}

    @app.post("/v1/ensembles/{group_id}/research")
    def research_ensemble(group_id: str):
        try:
            return {"build": service.research(group_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"资料整理失败：{exc}") from exc

    @app.post("/v1/ensembles/{group_id}/confirm")
    def confirm_ensemble(group_id: str, req: EnsembleConfirmRequest):
        try:
            return {
                "build": service.confirm(
                    group_id,
                    req.selected_indices,
                    confirm_over_soft_limit=req.confirm_over_soft_limit,
                )
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            detail_fn = getattr(exc, "detail", None)
            if callable(detail_fn):
                raise HTTPException(status_code=409, detail=detail_fn()) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"群像创建失败：{exc}") from exc

    @app.post("/v1/ensembles/{group_id}/cancel")
    def cancel_ensemble(group_id: str):
        try:
            return {"build": service.cancel(group_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app
