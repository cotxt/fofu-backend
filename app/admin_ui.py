# ruff: noqa: E501 -- HTML, CSS, and JavaScript assets are intentionally embedded verbatim.

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

router = APIRouter(include_in_schema=False)

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; font-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; img-src 'self' data: blob:; "
        "object-src 'none'; script-src 'self'; style-src 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}

_ADMIN_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <meta name="robots" content="noindex,nofollow,noarchive">
  <title>Fofu Admin</title>
  <link rel="stylesheet" href="/admin/assets/admin.css">
  <script src="/admin/assets/admin.js" defer></script>
</head>
<body>
  <main class="login-shell" id="login-view">
    <section class="login-card" aria-labelledby="login-title">
      <div class="brand-mark" aria-hidden="true">F</div>
      <p class="eyebrow">FOFU OPERATIONS</p>
      <h1 id="login-title">관리자 로그인</h1>
      <p class="muted">점주 신청과 서비스 운영 현황을 안전하게 관리합니다.</p>
      <form id="login-form">
        <label for="email">이메일</label>
        <input id="email" name="email" type="email" autocomplete="username" required>
        <label for="password">비밀번호</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required>
        <p class="form-error" id="login-error" role="alert" hidden></p>
        <button class="primary full" type="submit" id="login-button">로그인</button>
      </form>
    </section>
  </main>

  <div class="admin-shell" id="admin-view" hidden>
    <aside class="sidebar">
      <div class="brand-row"><span class="brand-mark small" aria-hidden="true">F</span><strong>Fofu Admin</strong></div>
      <nav id="navigation" aria-label="관리자 메뉴">
        <button class="nav-item active" type="button" data-section="overview">Overview</button>
        <button class="nav-item" type="button" data-section="applications">Owner applications</button>
        <button class="nav-item" type="button" data-section="users">Users</button>
        <button class="nav-item" type="button" data-section="restaurants">Restaurants</button>
        <button class="nav-item" type="button" data-section="audit">Audit log</button>
      </nav>
      <div class="account-block">
        <span class="muted tiny">SIGNED IN AS</span>
        <strong id="account-name">Administrator</strong>
        <span id="account-email" class="muted tiny"></span>
        <button class="ghost" type="button" id="logout-button">Log out</button>
      </div>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <div>
          <p class="eyebrow">FOFU OPERATIONS</p>
          <h1 id="section-title">Overview</h1>
        </div>
        <div class="topbar-actions">
          <span class="environment">ADMIN</span>
          <button class="secondary mobile-logout" type="button" id="mobile-logout-button">Log out</button>
        </div>
      </header>
      <p class="notice" id="notice" role="status" hidden></p>
      <div id="content" aria-live="polite"></div>
    </section>
  </div>
</body>
</html>
"""

_ADMIN_CSS = """
:root {
  color: #172019;
  background: #f4f1e9;
  font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-synthesis: none;
  --ink: #172019;
  --muted: #6b726b;
  --line: #dedbd2;
  --paper: #fffdf8;
  --cream: #f4f1e9;
  --green: #315b42;
  --green-soft: #e3eee6;
  --red: #9c3c32;
  --red-soft: #f8e8e5;
  --gold: #7a5c1e;
  --gold-soft: #f5ecd7;
}
* { box-sizing: border-box; }
html, body { min-height: 100%; margin: 0; }
body { min-height: 100vh; min-height: 100dvh; background: var(--cream); }
button, input, select, textarea { color: inherit; font: inherit; }
button { cursor: pointer; }
[hidden] { display: none !important; }
.login-shell { min-height: 100vh; min-height: 100dvh; display: grid; place-items: center; padding: max(24px, env(safe-area-inset-top)) 20px max(24px, env(safe-area-inset-bottom)); }
.login-card { width: min(100%, 430px); padding: 42px; border: 1px solid var(--line); border-radius: 28px; background: var(--paper); box-shadow: 0 24px 70px rgba(34, 43, 36, .09); }
.brand-mark { width: 48px; height: 48px; display: grid; place-items: center; border-radius: 15px; background: var(--green); color: white; font-family: Georgia, serif; font-size: 27px; font-style: italic; }
.brand-mark.small { width: 36px; height: 36px; border-radius: 11px; font-size: 21px; }
.brand-row { display: flex; align-items: center; gap: 12px; font-size: 18px; }
.eyebrow { margin: 24px 0 7px; color: var(--green); font-size: 11px; font-weight: 800; letter-spacing: .15em; }
h1, h2, h3, p { overflow-wrap: anywhere; }
h1 { margin: 0; font-family: Georgia, "Times New Roman", serif; font-size: clamp(31px, 5vw, 44px); font-weight: 500; letter-spacing: -.025em; }
h2 { margin: 0; font-family: Georgia, "Times New Roman", serif; font-size: 25px; font-weight: 500; }
h3 { margin: 0; font-size: 16px; }
.muted { color: var(--muted); line-height: 1.55; }
.tiny { font-size: 11px; letter-spacing: .04em; }
form { display: grid; gap: 10px; margin-top: 28px; }
label { margin-top: 7px; font-size: 12px; font-weight: 750; }
input, select, textarea { width: 100%; border: 1px solid var(--line); border-radius: 11px; background: white; padding: 12px 13px; outline: none; }
input:focus, select:focus, textarea:focus { border-color: var(--green); box-shadow: 0 0 0 3px rgba(49, 91, 66, .12); }
textarea { min-height: 82px; resize: vertical; }
button { border: 0; border-radius: 11px; padding: 11px 14px; font-weight: 750; }
button:disabled { cursor: wait; opacity: .55; }
.primary { background: var(--green); color: white; }
.primary:hover { background: #244c35; }
.secondary { border: 1px solid var(--line); background: white; }
.danger { border: 1px solid #e4bdb8; background: var(--red-soft); color: var(--red); }
.ghost { border: 1px solid rgba(255, 255, 255, .18); background: transparent; color: white; }
.full { width: 100%; margin-top: 13px; }
.form-error, .notice { margin: 8px 0 0; border-radius: 10px; padding: 11px 13px; background: var(--red-soft); color: var(--red); font-size: 13px; }
.notice.success { background: var(--green-soft); color: var(--green); }
.admin-shell { min-height: 100vh; min-height: 100dvh; display: grid; grid-template-columns: 250px minmax(0, 1fr); }
.sidebar { position: sticky; top: 0; height: 100vh; height: 100dvh; display: flex; flex-direction: column; padding: max(25px, env(safe-area-inset-top)) 20px max(20px, env(safe-area-inset-bottom)); background: #17251d; color: white; }
nav { display: grid; gap: 5px; margin-top: 42px; }
.nav-item { border-radius: 10px; background: transparent; color: #bdc8c0; text-align: left; font-size: 13px; }
.nav-item:hover, .nav-item.active { background: rgba(255, 255, 255, .1); color: white; }
.account-block { display: grid; gap: 6px; margin-top: auto; padding-top: 18px; border-top: 1px solid rgba(255, 255, 255, .13); }
.account-block .muted { color: #a6b2a9; }
.account-block .ghost { margin-top: 9px; }
.workspace { min-width: 0; padding: max(36px, env(safe-area-inset-top)) clamp(22px, 5vw, 72px) max(45px, env(safe-area-inset-bottom)); }
.topbar { display: flex; align-items: end; justify-content: space-between; gap: 20px; margin-bottom: 30px; }
.topbar-actions { display: flex; align-items: center; gap: 9px; }
.mobile-logout { display: none; }
.topbar .eyebrow { margin-top: 0; }
.environment { border: 1px solid #c9d8cd; border-radius: 999px; background: var(--green-soft); color: var(--green); padding: 7px 11px; font-size: 10px; font-weight: 850; letter-spacing: .12em; }
.stats { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 14px; }
.stat, .panel, .application-card { border: 1px solid var(--line); border-radius: 18px; background: var(--paper); }
.stat { min-height: 135px; padding: 21px; }
.stat strong { display: block; margin-top: 18px; font-family: Georgia, serif; font-size: 36px; font-weight: 500; }
.stat span { color: var(--muted); font-size: 12px; }
.panel { margin-top: 18px; overflow: hidden; }
.panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 20px 22px; border-bottom: 1px solid var(--line); }
.panel-heading p { margin: 4px 0 0; font-size: 12px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; min-width: 700px; }
th, td { padding: 14px 18px; border-bottom: 1px solid #ece9e1; text-align: left; vertical-align: top; font-size: 12px; }
th { background: #faf8f2; color: var(--muted); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
tr:last-child td { border-bottom: 0; }
.status { display: inline-flex; border-radius: 999px; padding: 5px 9px; background: #ecece8; color: #555d56; font-size: 10px; font-weight: 800; text-transform: uppercase; }
.status.approved, .status.active, .status.published, .status.verified, .status.open { background: var(--green-soft); color: var(--green); }
.status.rejected, .status.inactive, .status.closed { background: var(--red-soft); color: var(--red); }
.status.under_review, .status.pending, .status.private, .status.unverified { background: var(--gold-soft); color: var(--gold); }
.applications { display: grid; gap: 15px; }
.application-card { padding: 22px; }
.application-header, .application-actions, .field-grid { display: flex; gap: 12px; }
.application-header { align-items: start; justify-content: space-between; }
.application-card .meta { margin: 7px 0 0; color: var(--muted); font-size: 12px; }
.application-details { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 13px; margin: 20px 0; padding: 16px; border-radius: 13px; background: #f8f6ef; }
.detail-label { display: block; margin-bottom: 4px; color: var(--muted); font-size: 10px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; }
.detail-value { font-size: 12px; }
.review-controls { display: grid; grid-template-columns: minmax(180px, .8fr) minmax(220px, 1.4fr); gap: 10px; }
.application-actions { flex-wrap: wrap; margin-top: 12px; }
.searchbar { display: grid; grid-template-columns: minmax(180px, 1fr) auto; gap: 9px; margin-bottom: 14px; }
.table-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--line); }
.table-toolbar p { margin: 0; font-size: 11px; }
.moderation-controls { display: grid; gap: 7px; min-width: 126px; }
.moderation-state { display: flex; align-items: center; justify-content: space-between; gap: 7px; }
.moderation-controls button { padding: 7px 9px; font-size: 10px; white-space: nowrap; }
.pager { display: flex; align-items: center; justify-content: flex-end; gap: 9px; padding: 14px 18px; border-top: 1px solid var(--line); }
.pager span { margin-right: auto; color: var(--muted); font-size: 11px; }
.pager button { border: 1px solid var(--line); background: white; padding: 8px 11px; }
.empty, .loading { padding: 50px 24px; color: var(--muted); text-align: center; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
@media (max-width: 1050px) {
  .stats { grid-template-columns: repeat(2, minmax(140px, 1fr)); }
  .application-details { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .admin-shell { display: block; }
  .sidebar { position: static; width: 100%; height: auto; padding: max(18px, env(safe-area-inset-top)) 16px 14px; }
  nav { display: flex; overflow-x: auto; margin: 17px -4px 0; padding: 0 4px 4px; }
  .nav-item { flex: 0 0 auto; white-space: nowrap; }
  .account-block { display: none; }
  .workspace { padding: 25px 16px max(32px, env(safe-area-inset-bottom)); }
  .topbar { align-items: center; margin-bottom: 22px; }
  .mobile-logout { display: inline-flex; }
  .topbar h1 { font-size: 32px; }
  .application-details, .review-controls { grid-template-columns: 1fr; }
}
@media (max-width: 460px) {
  .login-card { padding: 30px 23px; border-radius: 22px; }
  .stats { grid-template-columns: 1fr 1fr; gap: 9px; }
  .stat { min-height: 112px; padding: 16px; }
  .stat strong { margin-top: 13px; font-size: 30px; }
  .environment { display: none; }
}
"""

_ADMIN_JS = r"""
(() => {
  "use strict";

  const LOGIN_URL = "/api/v1/admin/auth/login";
  const REFRESH_URL = "/api/v1/admin/auth/refresh";
  const LOGOUT_URL = "/api/v1/admin/auth/logout";
  const ADMIN_API = "/api/v1/admin";
  const PAGE_SIZE = 50;
  let accessToken = null;
  let currentUser = null;
  let restaurants = [];
  let refreshInFlight = null;
  let restaurantQuery = "";
  const offsets = { users: 0, restaurants: 0, applications: 0, audit: 0 };

  const byId = (id) => document.getElementById(id);
  const loginView = byId("login-view");
  const adminView = byId("admin-view");
  const content = byId("content");
  const notice = byId("notice");
  const titles = {
    overview: "Overview",
    applications: "Owner applications",
    users: "Users",
    restaurants: "Restaurants",
    audit: "Audit log",
  };

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }

  function actionButton(label, className, handler) {
    const item = node("button", className, label);
    item.type = "button";
    item.addEventListener("click", handler);
    return item;
  }

  function formatDate(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? String(value) : parsed.toLocaleString("ko-KR");
  }

  function setNotice(message, success = false) {
    notice.textContent = message || "";
    notice.className = success ? "notice success" : "notice";
    notice.hidden = !message;
  }

  function errorMessage(payload, fallback) {
    return payload && payload.error && payload.error.message ? payload.error.message : fallback;
  }

  async function responsePayload(response) {
    try { return await response.json(); } catch (_) { return null; }
  }

  async function refreshSession(silent = false) {
    if (!refreshInFlight) {
      refreshInFlight = (async () => {
        const response = await fetch(REFRESH_URL, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Accept": "application/json" },
        });
        if (!response.ok) {
          accessToken = null;
          currentUser = null;
          if (!silent) setNotice("세션을 다시 확인할 수 없습니다.");
          return false;
        }
        const payload = await response.json();
        if (!payload.user || !Array.isArray(payload.user.roles) || !payload.user.roles.includes("admin")) {
          accessToken = null;
          currentUser = null;
          return false;
        }
        accessToken = payload.access_token;
        currentUser = payload.user;
        return true;
      })();
    }
    try {
      return await refreshInFlight;
    } finally {
      refreshInFlight = null;
    }
  }

  async function authorizedFetch(path, options = {}, retry = true) {
    if (!accessToken) throw new Error("관리자 세션이 없습니다.");
    const headers = new Headers(options.headers || {});
    headers.set("Accept", headers.get("Accept") || "application/json");
    headers.set("Authorization", `Bearer ${accessToken}`);
    const response = await fetch(`${ADMIN_API}${path}`, {
      ...options,
      headers,
      credentials: "same-origin",
    });
    if (response.status === 401 && retry && await refreshSession(true)) {
      return authorizedFetch(path, options, false);
    }
    return response;
  }

  async function api(path, options = {}) {
    const response = await authorizedFetch(path, options);
    const payload = await responsePayload(response);
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) showLogin();
      throw new Error(errorMessage(payload, `요청을 완료하지 못했습니다 (${response.status}).`));
    }
    return payload;
  }

  function showLogin(message = "") {
    accessToken = null;
    currentUser = null;
    adminView.hidden = true;
    loginView.hidden = false;
    byId("password").value = "";
    const loginError = byId("login-error");
    loginError.textContent = message;
    loginError.hidden = !message;
  }

  function showAdmin() {
    loginView.hidden = true;
    adminView.hidden = false;
    byId("account-name").textContent = currentUser.display_name || "Administrator";
    byId("account-email").textContent = currentUser.email || "";
  }

  function loading() {
    content.replaceChildren(node("div", "loading", "데이터를 불러오는 중입니다…"));
  }

  function empty(message) {
    return node("div", "empty", message);
  }

  function statusBadge(value) {
    return node("span", `status ${String(value || "").toLowerCase()}`, value || "unknown");
  }

  function panel(title, subtitle, body) {
    const wrapper = node("section", "panel");
    const heading = node("div", "panel-heading");
    const copy = node("div");
    copy.append(node("h2", "", title));
    if (subtitle) copy.append(node("p", "muted", subtitle));
    heading.append(copy);
    wrapper.append(heading, body);
    return wrapper;
  }

  function pager(section, total, render) {
    const wrapper = node("div", "pager");
    const offset = offsets[section];
    const start = total ? offset + 1 : 0;
    const end = Math.min(offset + PAGE_SIZE, total);
    wrapper.append(node("span", "", `${start}–${end} / ${total}`));
    const previous = actionButton("Previous", "", async () => {
      offsets[section] = Math.max(0, offset - PAGE_SIZE);
      await render();
    });
    previous.disabled = offset === 0;
    const next = actionButton("Next", "", async () => {
      offsets[section] = offset + PAGE_SIZE;
      await render();
    });
    next.disabled = offset + PAGE_SIZE >= total;
    wrapper.append(previous, next);
    return wrapper;
  }

  function table(headers, rows) {
    const wrap = node("div", "table-wrap");
    if (!rows.length) return empty("표시할 항목이 없습니다.");
    const element = node("table");
    const head = node("thead");
    const headRow = node("tr");
    headers.forEach((label) => headRow.append(node("th", "", label)));
    head.append(headRow);
    const body = node("tbody");
    rows.forEach((cells) => {
      const row = node("tr");
      cells.forEach((cell) => {
        const td = node("td");
        td.append(cell instanceof Node ? cell : document.createTextNode(String(cell ?? "—")));
        row.append(td);
      });
      body.append(row);
    });
    element.append(head, body);
    wrap.append(element);
    return wrap;
  }

  async function renderOverview() {
    loading();
    const data = await api("/overview");
    const labels = [
      ["전체 사용자", data.users_total],
      ["활성 사용자", data.users_active],
      ["전체 매장", data.restaurants_total],
      ["공개 매장", data.restaurants_published],
      ["대기 중 신청", data.owner_applications_pending],
      ["심사 중 신청", data.owner_applications_under_review],
      ["감사 이벤트", data.audit_events_total],
    ];
    const stats = node("div", "stats");
    labels.forEach(([label, value]) => {
      const card = node("article", "stat");
      card.append(node("span", "", label), node("strong", "", Number(value || 0).toLocaleString("ko-KR")));
      stats.append(card);
    });
    const guide = node("div", "empty", "승인 전 사업자 서류와 연결할 매장을 반드시 확인하세요. 승인된 매장은 자동 공개되지 않습니다.");
    content.replaceChildren(stats, panel("운영 안내", "권한 변경과 서류 열람은 감사 로그에 기록됩니다.", guide));
  }

  async function renderUsers() {
    loading();
    const data = await api(`/users?limit=${PAGE_SIZE}&offset=${offsets.users}`);
    const rows = data.items.map((user) => [
      node("strong", "", user.display_name),
      user.email || "—",
      (user.roles || []).join(", ") || "customer",
      statusBadge(user.is_active ? "active" : "inactive"),
      user.locale,
      formatDate(user.created_at),
    ]);
    const body = node("div");
    body.append(
      table(["Name", "Email", "Roles", "Status", "Locale", "Created"], rows),
      pager("users", data.total, renderUsers),
    );
    content.replaceChildren(panel("Users", `총 ${data.total}명`, body));
  }

  async function loadRestaurantOptions() {
    const query = restaurantQuery ? `&q=${encodeURIComponent(restaurantQuery)}` : "";
    const data = await api(`/restaurants?limit=100${query}`);
    restaurants = data.items || [];
    return data;
  }

  const moderationLabels = {
    is_published: { on: "공개", off: "비공개" },
    is_verified: { on: "검증 완료", off: "미검증" },
    is_open: { on: "영업 중", off: "휴무" },
  };

  async function updateRestaurantModeration(restaurant, field, value, button) {
    const labels = moderationLabels[field];
    if (!labels) return;
    const nextState = value ? labels.on : labels.off;
    const restaurantName = restaurant.name_ko || restaurant.name_en;
    if (!window.confirm(`${restaurantName}의 상태를 '${nextState}'(으)로 변경하시겠습니까?`)) return;

    const controls = button.closest(".moderation-controls");
    const buttons = controls ? Array.from(controls.querySelectorAll("button")) : [button];
    buttons.forEach((item) => { item.disabled = true; });
    let saved = false;
    try {
      await api(`/restaurants/${encodeURIComponent(restaurant.id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: value }),
      });
      saved = true;
      await renderRestaurants();
      setNotice(`${restaurantName}의 상태를 '${nextState}'(으)로 변경했습니다. 감사 로그에 기록되었습니다.`, true);
    } catch (error) {
      setNotice(saved ? "변경은 저장됐지만 목록을 새로 불러오지 못했습니다. Refresh를 눌러 확인해주세요." : error.message);
      if (!saved) buttons.forEach((item) => { item.disabled = false; });
    }
  }

  function moderationControl(restaurant, field) {
    const value = Boolean(restaurant[field]);
    const labels = moderationLabels[field];
    const wrapper = node("div", "moderation-controls");
    const state = node("div", "moderation-state");
    const badgeValue = field === "is_published"
      ? (value ? "published" : "private")
      : field === "is_verified"
        ? (value ? "verified" : "unverified")
        : (value ? "open" : "closed");
    state.append(statusBadge(badgeValue));
    const button = actionButton(value ? `${labels.off} 전환` : `${labels.on} 전환`, value ? "secondary" : "primary", (event) => {
      updateRestaurantModeration(restaurant, field, !value, event.currentTarget);
    });
    wrapper.append(state, button);
    return wrapper;
  }

  async function renderRestaurants() {
    loading();
    const data = await api(`/restaurants?limit=${PAGE_SIZE}&offset=${offsets.restaurants}`);
    const rows = data.items.map((restaurant) => [
      node("strong", "", restaurant.name_ko || restaurant.name_en),
      node("span", "mono", restaurant.slug),
      restaurant.owner_user_id || "—",
      moderationControl(restaurant, "is_verified"),
      moderationControl(restaurant, "is_published"),
      moderationControl(restaurant, "is_open"),
      formatDate(restaurant.created_at),
    ]);
    const body = node("div");
    const toolbar = node("div", "table-toolbar");
    toolbar.append(
      node("p", "muted", "공개 전에는 점주 승인, 메뉴, 검증 및 영업 상태를 확인하세요."),
      actionButton("Refresh", "secondary", async (event) => {
        event.currentTarget.disabled = true;
        try {
          await renderRestaurants();
          setNotice("식당 상태를 새로 불러왔습니다.", true);
        } catch (error) {
          setNotice(error.message);
          event.currentTarget.disabled = false;
        }
      }),
    );
    body.append(
      toolbar,
      table(["Restaurant", "Slug", "Owner ID", "Verification", "Visibility", "Open state", "Created"], rows),
      pager("restaurants", data.total, renderRestaurants),
    );
    content.replaceChildren(panel("Restaurants", `총 ${data.total}곳`, body));
  }

  function detail(label, value) {
    const item = node("div");
    item.append(node("span", "detail-label", label), node("span", "detail-value", value || "—"));
    return item;
  }

  async function reviewApplication(application, status, restaurantSelect, reviewNote, button) {
    if (status === "approved" && !restaurantSelect.value) {
      setNotice("승인할 기존 매장을 선택해주세요.");
      restaurantSelect.focus();
      return;
    }
    if (status === "rejected" && !reviewNote.value.trim()) {
      setNotice("거절 사유를 입력해주세요.");
      reviewNote.focus();
      return;
    }
    const verb = status === "approved" ? "승인" : status === "rejected" ? "거절" : "심사 중으로 변경";
    if (!window.confirm(`${application.business_name} 신청을 ${verb}하시겠습니까?`)) return;
    button.disabled = true;
    try {
      const payload = { status, review_note: reviewNote.value.trim() || null };
      if (status === "approved") payload.restaurant_id = restaurantSelect.value;
      await api(`/owner-applications/${encodeURIComponent(application.id)}/review`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setNotice(`신청 상태를 ${status}(으)로 변경했습니다.`, true);
      await renderApplications();
    } catch (error) {
      setNotice(error.message);
      button.disabled = false;
    }
  }

  async function downloadLicense(application, button) {
    button.disabled = true;
    try {
      const response = await authorizedFetch(`/owner-applications/${encodeURIComponent(application.id)}/license`);
      if (!response.ok) {
        const payload = await responsePayload(response);
        throw new Error(errorMessage(payload, "사업자 서류를 내려받지 못했습니다."));
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = application.license_original_filename || "business-license";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setNotice("사업자 서류를 내려받았습니다. 열람 기록이 감사 로그에 저장됩니다.", true);
    } catch (error) {
      setNotice(error.message);
    } finally {
      button.disabled = false;
    }
  }

  function applicationCard(application) {
    const card = node("article", "application-card");
    const heading = node("div", "application-header");
    const copy = node("div");
    copy.append(node("h3", "", application.business_name));
    copy.append(node("p", "meta", `${application.applicant_display_name} · ${application.applicant_email || "이메일 없음"} · ${formatDate(application.created_at)}`));
    heading.append(copy, statusBadge(application.status));

    const details = node("div", "application-details");
    details.append(
      detail("Registration", application.registration_number),
      detail("Phone", application.phone),
      detail("Address", application.address),
      detail("Linked restaurant", application.restaurant_name),
      detail("Terms", application.terms_version),
      detail("Review note", application.review_note),
    );
    card.append(heading, details);

    const controls = node("div", "review-controls");
    const restaurantSelect = node("select");
    restaurantSelect.setAttribute("aria-label", "연결할 매장");
    restaurantSelect.append(node("option", "", "연결할 기존 매장을 선택하세요"));
    restaurantSelect.firstChild.value = "";
    restaurants.forEach((restaurant) => {
      const option = node("option", "", restaurant.name_ko || restaurant.name_en);
      option.value = restaurant.id;
      if (restaurant.id === application.restaurant_id) option.selected = true;
      restaurantSelect.append(option);
    });
    const reviewNote = node("textarea");
    reviewNote.placeholder = "심사 메모 또는 거절 사유";
    reviewNote.setAttribute("aria-label", "심사 메모");
    reviewNote.value = application.review_note || "";
    controls.append(restaurantSelect, reviewNote);
    card.append(controls);

    const actions = node("div", "application-actions");
    actions.append(actionButton("서류 다운로드", "secondary", (event) => downloadLicense(application, event.currentTarget)));
    if (!(["approved", "rejected"].includes(application.status))) {
      actions.append(
        actionButton("심사 중", "secondary", (event) => reviewApplication(application, "under_review", restaurantSelect, reviewNote, event.currentTarget)),
        actionButton("승인", "primary", (event) => reviewApplication(application, "approved", restaurantSelect, reviewNote, event.currentTarget)),
        actionButton("거절", "danger", (event) => reviewApplication(application, "rejected", restaurantSelect, reviewNote, event.currentTarget)),
      );
    }
    card.append(actions);
    return card;
  }

  async function renderApplications() {
    loading();
    const [applications] = await Promise.all([
      api(`/owner-applications?limit=${PAGE_SIZE}&offset=${offsets.applications}`),
      loadRestaurantOptions(),
    ]);
    const searchbar = node("div", "searchbar");
    const search = node("input");
    search.type = "search";
    search.placeholder = "승인에 연결할 매장명 또는 slug 검색";
    search.value = restaurantQuery;
    search.setAttribute("aria-label", "연결할 매장 검색");
    const searchButton = actionButton("매장 검색", "secondary", async () => {
      restaurantQuery = search.value.trim();
      await renderApplications();
    });
    search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); searchButton.click(); }
    });
    searchbar.append(search, searchButton);
    const list = node("div", "applications");
    if (!applications.items.length) list.append(empty("점주 신청이 없습니다."));
    applications.items.forEach((application) => list.append(applicationCard(application)));
    list.append(pager("applications", applications.total, renderApplications));
    content.replaceChildren(searchbar, list);
  }

  async function renderAudit() {
    loading();
    const data = await api(`/audit-events?limit=${PAGE_SIZE}&offset=${offsets.audit}`);
    const rows = data.items.map((event) => [
      formatDate(event.created_at),
      node("strong", "", event.action),
      `${event.resource_type} · ${event.resource_id}`,
      event.actor_email || event.actor_user_id || "system",
      node("span", "mono", JSON.stringify(event.details || {})),
    ]);
    const body = node("div");
    body.append(
      table(["Time", "Action", "Resource", "Actor", "Details"], rows),
      pager("audit", data.total, renderAudit),
    );
    content.replaceChildren(panel("Audit log", `전체 ${data.total}건`, body));
  }

  async function loadSection(section) {
    setNotice("");
    byId("section-title").textContent = titles[section] || "Overview";
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.section === section));
    try {
      if (section === "applications") await renderApplications();
      else if (section === "users") await renderUsers();
      else if (section === "restaurants") await renderRestaurants();
      else if (section === "audit") await renderAudit();
      else await renderOverview();
    } catch (error) {
      content.replaceChildren(empty("데이터를 표시할 수 없습니다."));
      setNotice(error.message);
    }
  }

  byId("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = byId("login-button");
    const loginError = byId("login-error");
    button.disabled = true;
    loginError.hidden = true;
    try {
      const response = await fetch(LOGIN_URL, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Accept": "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({
          email: byId("email").value,
          password: byId("password").value,
        }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(errorMessage(payload, "로그인 정보를 확인해주세요."));
      if (!payload.user || !Array.isArray(payload.user.roles) || !payload.user.roles.includes("admin")) {
        await fetch(LOGOUT_URL, { method: "POST", credentials: "same-origin" });
        throw new Error("관리자 권한이 없는 계정입니다.");
      }
      accessToken = payload.access_token;
      currentUser = payload.user;
      byId("password").value = "";
      showAdmin();
      await loadSection("overview");
    } catch (error) {
      loginError.textContent = error.message;
      loginError.hidden = false;
      byId("password").value = "";
    } finally {
      button.disabled = false;
    }
  });

  byId("navigation").addEventListener("click", (event) => {
    const button = event.target.closest("[data-section]");
    if (button) loadSection(button.dataset.section);
  });

  async function logout() {
    const token = accessToken;
    accessToken = null;
    try {
      await fetch(LOGOUT_URL, {
        method: "POST",
        credentials: "same-origin",
        headers: token ? { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" } : { "Content-Type": "application/json" },
        body: "{}",
      });
    } finally {
      showLogin("로그아웃했습니다.");
    }
  }

  byId("logout-button").addEventListener("click", logout);
  byId("mobile-logout-button").addEventListener("click", logout);

  refreshSession(true).then(async (restored) => {
    if (!restored) { showLogin(); return; }
    showAdmin();
    await loadSection("overview");
  }).catch(() => showLogin());
})();
"""


@router.get("/admin", response_class=HTMLResponse)
@router.get("/admin/", response_class=HTMLResponse)
def admin_page() -> HTMLResponse:
    return HTMLResponse(_ADMIN_HTML, headers=_SECURITY_HEADERS)


@router.get("/admin/assets/admin.css")
def admin_css() -> Response:
    return Response(_ADMIN_CSS, media_type="text/css", headers=_SECURITY_HEADERS)


@router.get("/admin/assets/admin.js")
def admin_javascript() -> Response:
    return Response(
        _ADMIN_JS,
        media_type="application/javascript",
        headers=_SECURITY_HEADERS,
    )
