from fastapi import APIRouter, HTTPException, Request, Response, status

from app.auth import AuthService, SESSION_TTL_SECONDS
from app.schemas import AuthLoginRequest, AuthSessionResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=AuthSessionResponse)
def login(payload: AuthLoginRequest, request: Request, response: Response) -> AuthSessionResponse:
    client_ip = request.client.host if request.client else None
    with request.app.state.session_factory() as session:
        result = AuthService(session, request.app.state.settings).login(
            payload.username, payload.password, client_ip or ""
        )
    if result.rate_limited:
        raise HTTPException(
            status_code=429, detail="\u767b\u5f55\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41"
        )
    if not result.succeeded:
        raise HTTPException(
            status_code=401, detail="\u7528\u6237\u540d\u6216\u5bc6\u7801\u9519\u8bef"
        )
    response.set_cookie(
        key="demo_session",
        value=result.raw_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return AuthSessionResponse(username=result.principal.username)


@router.get("/session", response_model=AuthSessionResponse)
def current_session(request: Request) -> AuthSessionResponse:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="\u8bf7\u5148\u767b\u5f55")
    return AuthSessionResponse(username=principal.username)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request) -> Response:
    raw_token = request.cookies.get("demo_session")
    with request.app.state.session_factory() as session:
        AuthService(session, request.app.state.settings).logout(raw_token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie("demo_session")
    return response
