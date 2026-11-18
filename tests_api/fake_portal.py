"""Fake Powerquartier portal reproducing the formats observed on 18 Nov 2026."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from aiohttp import web

ACCOUNTS = [
    {"id": "aaaaaaaa-0000-0000-0000-000000000001", "name": "Profil-C-consumer"},
    {"id": "aaaaaaaa-0000-0000-0000-000000000002", "name": "Prüfprofil-B"},
    {"id": "aaaaaaaa-0000-0000-0000-000000000003", "name": "Profil-A"},
]
COMMUNITY = "00000000-1111-2222-3333-444444444444"
# Wh per 15-minute slot by profile: (community, grid)
WH = {
    ACCOUNTS[0]["id"]: (10.0, 5.0),
    ACCOUNTS[1]["id"]: (20.0, 10.0),
    ACCOUNTS[2]["id"]: (100.0, 50.0),
}
EMAIL, PASSWORD = "user@example.com", "test-password"  # pragma: allowlist secret


def _line(ident: str, payload) -> str:
    return f"{ident}:{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"


def _shell_lines() -> list[str]:
    return [
        '0:{"q":"","b":"x"}',
        _line("1b", ["$", "$L45", None, {"appMetadata": {"logoutRedirectUrl": "/auth/signin"}}]),
        '12:I[123,["$2","$3"],"default"]',
    ]


def _accounts_line(selected_id: str) -> str:
    accs = []
    for a in ACCOUNTS:
        accs.append(
            {
                "id": a["id"],
                "createdAt": "2025-01-01T00:00:00.000Z",
                "name": a["name"],
                "paymentDetails": {"id": "x"},
                "contracts": [{"community": COMMUNITY, "type": "consumer"}],
                "communities": ["$53:props:extendedAccounts:0:communities:0"],
                "isSelected": a["id"] == selected_id,
                "isActiveAccount": True,
            }
        )
    return _line("4e", ["$", "$L53", None, {"extendedAccounts": accs, "selectedAccount": "$53:props:extendedAccounts:2"}])


RATEPLAN = {
    "id": "rateplan-1",
    "name": "Beispieltarif",
    "boundTo": "community",
    "validBegin": "2024-12-31T23:00:00+00:00",
    "validEnd": "9999-01-01T00:00:00+00:00",
    "priceSpecs": [
        {"description": "Bezugstarif", "applicableTo": "consumption", "applicableWhere": "community",
         "priceClass": "fixedRate", "priceClassConfig": {"rate": {"amount": 0.12, "currency": "EUR"}, "price": None}},
        {"description": "Einspeisetarif", "applicableTo": "production", "applicableWhere": "community",
         "priceClass": "fixedRate", "priceClassConfig": {"rate": {"amount": 0.08, "currency": "EUR"}, "price": None}},
    ],
}


def _parse_param(request: web.Request, key: str) -> datetime:
    return datetime.fromisoformat(json.loads(request.query[key]).replace("Z", "+00:00"))


class FakePortal:
    def __init__(self) -> None:
        self.app = web.Application(middlewares=[self._rsc_redirect])
        r = self.app.router
        r.add_get("/self-service/login/browser", self.login_browser)
        r.add_post("/self-service/login", self.login_post)
        r.add_get("/sessions/whoami", self.whoami)
        r.add_get("/de-AT/account", self.account)
        r.add_get("/de-AT/auth/select", self.select_page)
        r.add_get("/de-AT/auth/signin", self.signin)
        r.add_get("/de-AT/analyse/you", self.analyse)
        r.add_get("/de-AT", self.overview)
        self.base = ""
        self.action_override: str | None = None
        self.sessions: set[str] = set()
        self.day_requests: list[tuple[str, datetime, datetime]] = []
        self.empty_days: set[str] = set()  # ISO dates (UTC start) returning zeros
        self.expire_next = False
        self.rsc_redirects = 0
        self.flaky: dict[str, int] = {}
        self.fail_5xx = 0  # next N RSC requests answer 503  # ISO date (UTC start) -> chartless answers left

    @web.middleware
    async def _rsc_redirect(self, request: web.Request, handler):
        """Like the real portal: an RSC request without _rsc is redirected
        to the same URL with ?_rsc=<hash> added."""
        if request.headers.get("RSC") == "1" and self.fail_5xx > 0:
            self.fail_5xx -= 1
            return web.Response(status=503, text="Service Unavailable")
        if request.headers.get("RSC") == "1" and "_rsc" not in request.query:
            self.rsc_redirects += 1
            sep = "&" if request.query_string else "?"
            raise web.HTTPTemporaryRedirect(f"{request.path_qs}{sep}_rsc=x1y2z")
        return await handler(request)

    # -- auth --
    async def login_browser(self, request: web.Request) -> web.Response:
        assert request.headers.get("Accept") == "application/json"
        resp = web.json_response(
            {
                "id": "flow1",
                "ui": {
                    "action": self.action_override or f"{self.base}/self-service/login?flow=flow1",
                    "method": "POST",
                    "nodes": [
                        {"attributes": {"name": "csrf_token", "type": "hidden", "value": "csrf123"}},
                        {"attributes": {"name": "identifier", "type": "text"}},
                    ],
                },
            }
        )
        resp.set_cookie("csrf_token_x", "csrfcookie")
        return resp

    async def login_post(self, request: web.Request) -> web.Response:
        body = await request.json()
        if request.cookies.get("csrf_token_x") != "csrfcookie" or body.get("csrf_token") != "csrf123":
            return web.json_response({"error": {"reason": "csrf"}}, status=403)
        if body.get("identifier") != EMAIL or body.get("password") != PASSWORD:
            return web.json_response(
                {"ui": {"messages": [{"id": 4000006, "text": "The provided credentials are invalid."}]}},
                status=400,
            )
        sid = f"s{len(self.sessions) + 1}"
        self.sessions.add(sid)
        resp = web.json_response({"session": {"active": True}})
        resp.set_cookie("ory_kratos_session", sid)
        return resp

    def _authed(self, request: web.Request) -> bool:
        if self.expire_next:
            self.expire_next = False
            self.sessions.clear()
        return request.cookies.get("ory_kratos_session") in self.sessions

    async def whoami(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"error": {"code": 401}}, status=401)
        return web.json_response({"active": True, "expires_at": "2099-01-01T00:00:00Z"})

    def _selected(self, request: web.Request) -> str | None:
        """Like the real portal: no profile chosen right after login."""
        return request.cookies.get("exnaton.selectedAccountId")

    def _select_lines(self) -> list[str]:
        accs = [
            {"id": a["id"], "name": a["name"], "type": "consumer",
             "paymentDetails": {"iban": "AT000000000000000000"},
             "metadata": {"bglFirstName": "X"}, "community": COMMUNITY}
            for a in ACCOUNTS
        ]
        return _shell_lines() + [
            _line("31", ["$", "$L33", None, {"title": "Select a profile", "children": [
                ["$", "$L35", None, {"selectedAccountId": ACCOUNTS[0]["id"], "accounts": accs}]]}])
        ]

    async def select_page(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return self._redirect()
        return web.Response(text="\n".join(self._select_lines()), content_type="text/x-component")

    def _redirect(self) -> web.Response:
        return web.Response(status=307, headers={"Location": "/de-AT/auth/signin"})

    async def signin(self, request: web.Request) -> web.Response:
        return web.Response(text="\n".join(_shell_lines()), content_type="text/x-component")

    # -- pages --
    async def account(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return self._redirect()
        if self._selected(request) is None:
            return web.Response(status=307, headers={"Location": "/de-AT/auth/select"})
        lines = _shell_lines() + [
            _accounts_line(self._selected(request)),
            '59:"TariffBreakdownCard"',
            _line("38", [["$", "$L5a", None, {"ratePlan": RATEPLAN}], False, None]),
        ]
        return web.Response(text="\n".join(lines), content_type="text/x-component")

    async def analyse(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return self._redirect()
        if self._selected(request) is None:
            return web.Response(status=307, headers={"Location": "/de-AT/auth/select"})
        start, stop = _parse_param(request, "start"), _parse_param(request, "stop")
        assert json.loads(request.query["dpMode"]) == "day"
        sel = self._selected(request)
        self.day_requests.append((sel, start, stop))
        key = start.date().isoformat()
        if self.flaky.get(key, 0) > 0:  # portal hiccup: page without chart
            self.flaky[key] -= 1
            return web.Response(text="\n".join(_shell_lines()), content_type="text/x-component")
        com, grid = WH[sel]
        if start.date().isoformat() in self.empty_days:
            com, grid = 0.0, 0.0
        pts = []
        t = start
        while t <= stop:
            pts.append(int(t.timestamp() * 1000))
            t += timedelta(minutes=15)

        def series(aspect, wh):
            return {
                "aspect": aspect, "interval": "15m", "stack": "energy-consumption",
                "rawData": [{"value": [ts, wh], "extras": {"dashed": True, "awaitingDataHighlight": False}} for ts in pts],
            }

        chart = {
            "dataType": "energy", "seriesType": "communityConsumption",
            "unit": {"name": "kWh", "rawValue": 195},
            "series": [series("communityConsumption", com), series("gridConsumption", grid)],
        }
        lines = _shell_lines() + [
            _accounts_line(sel),
            '83:I[734798,["$2"],"MultiCharts"]',
            _line("7d", ["$", "$L83", None, {"charts": {"dense": [chart], "sparse": [chart]}}]),
        ]
        return web.Response(text="\n".join(lines), content_type="text/x-component")

    async def overview(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return self._redirect()
        if self._selected(request) is None:
            return web.Response(status=307, headers={"Location": "/de-AT/auth/select"})
        assert request.query.get("communityId") == COMMUNITY
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        now = now - timedelta(minutes=now.minute % 15)
        data = []
        for i in range(-8, 200):
            ts = now + timedelta(minutes=15 * i)
            avail = "community" if 4 <= i < 12 else ("default" if i < 0 else "grid")
            data.append(
                {
                    "timestamp": int(ts.timestamp() * 1000),
                    "consumption": 7223972.17, "production": "$-0" if i < 0 else 3551537.43,
                    "surplus": -1.0, "availability": avail, "forecast": i >= 0, "preliminary": i < 0,
                    "communityConsumptionPrice": {"scale": 8, "value": 10000000},
                }
            )
        lines = _shell_lines() + [
            _accounts_line(self._selected(request)),
            _line("6a", ["$", "$L7c", None, {"withForecast": True, "withConsumptionPrice": True, "data": data, "nowIndex": 8}]),
        ]
        return web.Response(text="\n".join(lines), content_type="text/x-component")
