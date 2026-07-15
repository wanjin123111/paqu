(() => {
  "use strict";

  const PAGE_SIZE = 18;
  const SESSION_KEY = "paqu_schedule_secret";
  const $ = (id) => document.getElementById(id);
  const number = (value) => Number(value || 0);
  const text = (value) => String(value ?? "").trim();
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
  const pick = (object, keys, fallback = "") => {
    for (const key of keys) {
      if (object && object[key] !== undefined && object[key] !== null && object[key] !== "") return object[key];
    }
    return fallback;
  };
  const emptyCatalog = () => ({ version: 2, revision: 0, updated_at: "", dramas: {}, sources: {} });

  const state = {
    catalog: emptyCatalog(),
    sources: [],
    sourceMap: new Map(),
    accounts: [],
    backendAccounts: [],
    generatedAt: "",
    storage: "",
    reviewPage: 1,
    verified: false,
    saving: false,
    activeView: "dashboard",
    editorSourceKeys: [],
    editorDramaId: "",
  };

  const titles = {
    dashboard: ["运营工作台", "管理监控账号、认领公司作品并维护短剧资料"],
    accounts: ["监控账号", "查看最新抓取状态并向后端监控池添加账号"],
    review: ["作品认领", "判断抓取作品的公司归属，并支持跨账号合并"],
    dramas: ["公司短剧", "维护主创资料、来源绑定、上架状态和展示顺序"],
    settings: ["后台设置", "查看数据连接与正式配置的保存状态"],
  };

  function secret() {
    return sessionStorage.getItem(SESSION_KEY) || "";
  }

  function authHeaders(json = false) {
    const headers = {};
    if (secret()) headers["X-Schedule-Secret"] = secret();
    if (json) headers["Content-Type"] = "application/json";
    return headers;
  }

  async function api(url, options = {}) {
    const response = await fetch(url, { cache: "no-store", ...options });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.error || `请求失败（${response.status}）`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function toast(message, bad = false) {
    const node = $("toast");
    node.textContent = message;
    node.style.background = bad ? "#b93434" : "#171923";
    node.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => node.classList.remove("show"), 2600);
  }

  function setSync(message, ok = true) {
    $("syncText").textContent = message;
    const dot = document.querySelector(".status-pill .dot");
    if (dot) dot.style.background = ok ? "var(--green)" : "var(--red)";
  }

  function formatNumber(value) {
    const n = number(value);
    if (n >= 100000000) return `${(n / 100000000).toFixed(n >= 1000000000 ? 1 : 2).replace(/\.0+$/, "")}亿`;
    if (n >= 10000) return `${(n / 10000).toFixed(n >= 100000 ? 1 : 2).replace(/\.0+$/, "")}万`;
    return n.toLocaleString("zh-CN");
  }

  function formatTime(value) {
    if (!value) return "—";
    const normalized = String(value).replace(" ", "T");
    const date = new Date(normalized);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(date);
  }

  function cloneCatalog() {
    return JSON.parse(JSON.stringify(state.catalog));
  }

  function normalizeCatalog(catalog) {
    const clean = catalog && typeof catalog === "object" ? catalog : emptyCatalog();
    clean.version = 2;
    clean.revision = number(clean.revision);
    clean.updated_at = clean.updated_at || "";
    clean.dramas = clean.dramas && typeof clean.dramas === "object" ? clean.dramas : {};
    clean.sources = clean.sources && typeof clean.sources === "object" ? clean.sources : {};
    return clean;
  }

  function sourceKey(account, dramaId, title) {
    const cleanId = text(dramaId).replace(/^ID\s+/i, "");
    return `${text(account).toLowerCase()}|${cleanId || text(title).toLowerCase()}`;
  }

  function normalizePublicReport(report) {
    const sources = (report.dramas_detail || []).map((row) => {
      const account = text(pick(row, ["Account / 账号", "账号", "a"])).replace(/^@/, "");
      const english = text(pick(row, ["English Title / 英文剧名", "短剧名", "en"]));
      const chinese = text(pick(row, ["Chinese Title / 中文剧名", "中文剧名", "cn"]));
      const dramaId = text(pick(row, ["Drama ID / 短剧ID", "短剧ID", "id"])).replace(/^ID\s+/i, "");
      return {
        key: sourceKey(account, dramaId, english || chinese), account,
        nickname: text(pick(row, ["Nickname / 昵称", "昵称", "n"], account)),
        drama_id: dramaId, english_title: english, chinese_title: chinese,
        publish_time: text(pick(row, ["Publish Time / 发布时间", "发布时间", "p"])),
        episodes: number(pick(row, ["Episodes / 集数", "集数", "e"])),
        views: number(pick(row, ["Views / 观看数", "累计观看", "v"])),
        themes: text(pick(row, ["Chinese Themes / 中文题材", "English Themes / 英文题材", "ct", "et"])),
        description: text(pick(row, ["Chinese Description / 中文简介", "English Description Preview / 英文简介预览"])),
        link: text(pick(row, ["Drama Link / 短剧链接", "短剧链接"])),
        profile_url: text(pick(row, ["Source Profile URL / 来源主页", "主页链接"])),
      };
    }).filter((row) => row.account && row.key);
    const accounts = (report.summary || []).map((row) => ({
      account: text(pick(row, ["账号", "Account / 账号", "a"])).replace(/^@/, ""),
      nickname: text(pick(row, ["昵称", "Nickname / 昵称", "n"])),
      followers: number(pick(row, ["粉丝"])), dramas: number(pick(row, ["短剧数", "d"])),
      views: number(pick(row, ["累计观看", "v"])), profile_url: text(pick(row, ["主页链接"])),
    })).filter((row) => row.account);
    return { sources, accounts, generatedAt: report.generated_at || "" };
  }

  function setSourceRows(rows) {
    state.sources = Array.isArray(rows) ? rows : [];
    state.sourceMap = new Map(state.sources.map((row) => [row.key, row]));
  }

  function sourceStatus(key) {
    return state.catalog.sources[key]?.status || "pending";
  }

  function dramaSources(dramaId) {
    return Object.entries(state.catalog.sources)
      .filter(([, relation]) => relation?.status === "owned" && relation.drama_id === dramaId)
      .map(([key]) => state.sourceMap.get(key))
      .filter(Boolean);
  }

  function aggregateDramas() {
    return Object.entries(state.catalog.dramas).map(([id, drama]) => {
      const sources = dramaSources(id);
      return {
        id, ...drama, sources,
        accounts: [...new Set(sources.map((row) => row.account).filter(Boolean))],
        total_views: sources.reduce((sum, row) => sum + number(row.views), 0),
        episodes: Math.max(0, ...sources.map((row) => number(row.episodes))),
      };
    }).sort((a, b) => number(a.order || 999999) - number(b.order || 999999) || text(a.chinese_title).localeCompare(text(b.chinese_title), "zh-CN"));
  }

  function requireAuth(action = "进行这项操作") {
    if (state.verified) return true;
    $("authBox").classList.add("open");
    $("secretInput").focus();
    toast(`请先验证后台密码，再${action}`, true);
    return false;
  }

  async function loadPublicReport() {
    try {
      let report;
      let reportSource = "线上最新报表";
      try {
        report = await api(`/supabase/latest?t=${Date.now()}`);
      } catch (latestError) {
        reportSource = "静态备用报表";
        report = await api(`/public_reports/latest_report.json?t=${Date.now()}`);
      }
      const normalized = normalizePublicReport(report);
      if (!state.verified) {
        setSourceRows(normalized.sources);
        state.accounts = normalized.accounts;
        state.generatedAt = normalized.generatedAt;
        $("accountSource").textContent = `来源：${reportSource} ${normalized.accounts.length} 个账号`;
        renderAll();
      }
      setSync(`${reportSource}已载入`);
    } catch (error) {
      setSync("公开报表读取失败", false);
      toast(error.message, true);
    }
  }

  async function loadAdminCatalog(showMessage = false) {
    if (!secret()) return false;
    setSync("正在读取后台正式数据");
    try {
      const payload = await api(`/admin/catalog?t=${Date.now()}`, { headers: authHeaders() });
      state.catalog = normalizeCatalog(payload.catalog);
      state.storage = payload.storage || "";
      state.generatedAt = payload.generated_at || "";
      setSourceRows(payload.sources || []);
      state.accounts = payload.accounts || [];
      $("accountSource").textContent = `来源：线上最新报表 ${state.accounts.length} 个账号`;
      state.verified = true;
      $("authStatus").textContent = `验证成功 · 配置版本 ${state.catalog.revision}`;
      $("settingBackendState").textContent = "已验证";
      $("settingBackendState").className = "badge owned";
      updateStorageState();
      $("authBox").classList.remove("open");
      setSync("后台正式数据已同步");
      renderAll();
      loadBackendAccounts(false);
      if (showMessage) toast("后台数据已重新读取");
      return true;
    } catch (error) {
      state.verified = false;
      $("authStatus").textContent = error.status === 403 ? "密码不正确，请重新输入" : error.message;
      $("settingBackendState").textContent = "验证失败";
      $("settingBackendState").className = "badge pending";
      setSync("后台验证失败", false);
      if (showMessage) toast(error.message, true);
      return false;
    }
  }

  function updateStorageState() {
    const supabase = state.storage === "supabase";
    $("storageState").textContent = supabase ? "云端保存" : state.storage ? "运行时保存" : "待连接";
    $("storageState").className = `badge ${state.storage ? "owned" : "pending"}`;
    $("storageDetail").textContent = supabase
      ? `Supabase 正式保存 · 当前版本 ${state.catalog.revision}`
      : state.storage
        ? `Render 运行时文件备用保存 · 当前版本 ${state.catalog.revision}`
        : "等待验证后台连接";
  }

  async function saveCatalog(message) {
    if (!requireAuth("保存")) throw new Error("未验证");
    if (state.saving) throw new Error("上一项保存尚未完成");
    state.saving = true;
    setSync("正在保存后台正式数据");
    const expectedRevision = number(state.catalog.revision);
    try {
      const payload = await api("/admin/catalog", {
        method: "POST", headers: authHeaders(true),
        body: JSON.stringify({ expected_revision: expectedRevision, catalog: state.catalog }),
      });
      state.catalog = normalizeCatalog(payload.catalog);
      state.storage = payload.storage || state.storage;
      updateStorageState();
      setSync(`保存完成 · 版本 ${state.catalog.revision}`);
      renderAll();
      if (message) toast(message);
      return true;
    } catch (error) {
      if (error.status === 409 && error.payload?.catalog) {
        state.catalog = normalizeCatalog(error.payload.catalog);
        state.storage = error.payload.storage || state.storage;
        renderAll();
        toast("后台数据已被其他页面修改，已加载最新版本，请重新操作", true);
      } else {
        setSync("保存失败", false);
        toast(`保存失败：${error.message}`, true);
      }
      throw error;
    } finally {
      state.saving = false;
    }
  }

  async function mutateAndSave(mutator, message) {
    if (!requireAuth("修改数据")) return false;
    const before = cloneCatalog();
    try {
      mutator();
      renderAll();
      await saveCatalog(message);
      return true;
    } catch (error) {
      if (error.status !== 409) {
        state.catalog = before;
        renderAll();
      }
      return false;
    }
  }

  async function loadBackendAccounts(showMessage = true) {
    if (!requireAuth("读取后端账号池")) return;
    try {
      const payload = await api(`/schedule-accounts?t=${Date.now()}`, { headers: authHeaders() });
      state.backendAccounts = payload.accounts || [];
      $("accountSource").textContent = `来源：后端监控池 ${state.backendAccounts.length} 个账号`;
      renderAccounts();
      if (showMessage) toast(`已读取 ${state.backendAccounts.length} 个监控账号`);
    } catch (error) {
      toast(error.message, true);
    }
  }

  function switchView(name) {
    state.activeView = name;
    document.querySelectorAll(".view").forEach((node) => node.classList.toggle("active", node.id === `view-${name}`));
    document.querySelectorAll(".nav-btn[data-view]").forEach((node) => node.classList.toggle("active", node.dataset.view === name));
    $("pageTitle").textContent = titles[name][0];
    $("pageSub").textContent = titles[name][1];
    $("sidebar").classList.remove("open");
    if (name === "review") renderReview();
    if (name === "dramas") renderDramas();
    if (name === "accounts") renderAccounts();
  }

  function renderStats() {
    const dramas = aggregateDramas();
    const pending = state.sources.filter((row) => sourceStatus(row.key) === "pending").length;
    const accountCount = state.backendAccounts.length || state.accounts.length;
    $("statAccounts").textContent = accountCount || "0";
    $("statAccountsSub").textContent = state.backendAccounts.length ? "来自后端监控池" : "来自最新抓取报表";
    $("statPending").textContent = pending;
    $("statOwned").textContent = dramas.length;
    $("statLive").textContent = `${dramas.filter((row) => row.online).length} 部已上架`;
    $("statRaw").textContent = state.sources.length;
    $("statRawSub").textContent = `最新抓取共 ${state.sources.length} 条作品来源`;
    $("navAccountCount").textContent = accountCount;
    $("navPendingCount").textContent = pending;
    $("navOwnedCount").textContent = dramas.length;
    $("generatedText").textContent = state.generatedAt ? `最新抓取：${formatTime(state.generatedAt)}（北京时间）` : "尚未读取抓取结果";
  }

  function renderDashboard() {
    const pending = state.sources.filter((row) => sourceStatus(row.key) === "pending")
      .sort((a, b) => number(b.views) - number(a.views)).slice(0, 6);
    $("queueList").innerHTML = pending.length ? pending.map((row) => `
      <div class="queue-item"><div><div class="title-main">${escapeHtml(row.chinese_title || row.english_title || "未命名作品")}</div>
      <div class="title-sub">${escapeHtml(row.english_title)} · @${escapeHtml(row.account)}</div></div>
      <div class="metric">${formatNumber(row.views)}</div><span class="badge pending">待确认</span>
      <button class="btn small primary" data-edit-source="${escapeHtml(row.key)}">认领</button></div>`).join("")
      : `<div class="empty"><strong>没有待确认作品</strong>当前抓取结果已经处理完毕</div>`;

    const ranking = state.sources.slice().sort((a, b) => number(b.views) - number(a.views)).slice(0, 5);
    const max = Math.max(1, ...ranking.map((row) => number(row.views)));
    $("rankList").innerHTML = ranking.length ? ranking.map((row, index) => `
      <div class="rank-line"><div class="rank-no">${index + 1}</div><div><div class="title-main">${escapeHtml(row.chinese_title || row.english_title)}</div>
      <div class="bar"><i style="width:${Math.max(5, number(row.views) / max * 100)}%"></i></div></div>
      <div class="rank-value">${formatNumber(row.views)}</div></div>`).join("") : `<div class="empty">暂无数据</div>`;
  }

  function combinedAccounts() {
    const map = new Map(state.accounts.map((row) => [text(row.account).toLowerCase(), { ...row, monitored: false }]));
    for (const account of state.backendAccounts) {
      const key = text(account).toLowerCase();
      const current = map.get(key) || { account, nickname: account, followers: 0, dramas: 0, views: 0 };
      current.monitored = true;
      map.set(key, current);
    }
    return [...map.values()];
  }

  function renderAccounts() {
    const query = text($("accountSearch").value).toLowerCase();
    const filter = $("accountFilter").value;
    const rows = combinedAccounts().filter((row) => {
      const matched = !query || `${row.account} ${row.nickname}`.toLowerCase().includes(query);
      if (!matched) return false;
      if (filter === "healthy") return number(row.dramas) > 0;
      if (filter === "error") return number(row.dramas) === 0;
      return true;
    }).sort((a, b) => number(b.views) - number(a.views));
    $("accountGrid").innerHTML = rows.length ? rows.map((row) => `
      <article class="account-card"><div class="account-top"><div class="account-profile"><div class="avatar">${escapeHtml((row.nickname || row.account).slice(0, 2).toUpperCase())}</div>
      <div><div class="account-name">${escapeHtml(row.nickname || row.account)}</div><div class="account-handle">@${escapeHtml(row.account)}</div></div></div>
      <span class="badge ${number(row.dramas) ? "owned" : "pending"}">${number(row.dramas) ? "抓取正常" : "等待数据"}</span></div>
      <div class="account-meta"><div><strong>${formatNumber(row.followers)}</strong>粉丝</div><div><strong>${formatNumber(row.dramas)}</strong>短剧</div><div><strong>${formatNumber(row.views)}</strong>累计播放</div></div></article>`).join("")
      : `<div class="card empty" style="grid-column:1/-1"><strong>没有找到账号</strong>请调整搜索条件或验证密码后读取后端账号池</div>`;
  }

  function reviewRows() {
    const query = text($("reviewSearch").value).toLowerCase();
    const filter = $("reviewFilter").value;
    const sort = $("reviewSort").value;
    const rows = state.sources.filter((row) => {
      const matched = !query || `${row.chinese_title} ${row.english_title} ${row.account} ${row.nickname}`.toLowerCase().includes(query);
      return matched && (filter === "all" || sourceStatus(row.key) === filter);
    });
    rows.sort(sort === "latest"
      ? (a, b) => text(b.publish_time).localeCompare(text(a.publish_time))
      : sort === "title"
        ? (a, b) => text(a.chinese_title || a.english_title).localeCompare(text(b.chinese_title || b.english_title), "zh-CN")
        : (a, b) => number(b.views) - number(a.views));
    return rows;
  }

  function renderReview() {
    const all = reviewRows();
    const pageCount = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
    state.reviewPage = Math.min(Math.max(1, state.reviewPage), pageCount);
    const rows = all.slice((state.reviewPage - 1) * PAGE_SIZE, state.reviewPage * PAGE_SIZE);
    $("reviewCount").textContent = `共 ${all.length} 条来源`;
    $("reviewPagerText").textContent = `第 ${state.reviewPage}/${pageCount} 页 · 共 ${all.length} 条`;
    $("reviewPrev").disabled = state.reviewPage <= 1;
    $("reviewNext").disabled = state.reviewPage >= pageCount;
    $("reviewCheckAll").checked = false;
    $("reviewBody").innerHTML = rows.length ? rows.map((row) => {
      const status = sourceStatus(row.key);
      const relation = state.catalog.sources[row.key];
      const drama = relation?.drama_id ? state.catalog.dramas[relation.drama_id] : null;
      return `<tr><td><input class="check review-check" type="checkbox" data-key="${escapeHtml(row.key)}" /></td>
        <td><div class="title-main">${escapeHtml(row.chinese_title || "中文名待补充")}</div><div class="title-sub">${escapeHtml(row.english_title || "英文名待补充")}</div>
        ${drama ? `<div class="title-sub" style="color:var(--purple)">已归入：${escapeHtml(drama.chinese_title || drama.english_title)}</div>` : ""}</td>
        <td><div class="account-name">${escapeHtml(row.nickname || row.account)}</div><div class="account-handle">@${escapeHtml(row.account)}</div></td>
        <td>${escapeHtml(formatTime(row.publish_time))}</td><td class="metric">${number(row.episodes)}</td><td class="metric">${formatNumber(row.views)}</td>
        <td><span class="badge ${status}">${status === "owned" ? "已认领" : status === "ignored" ? "已忽略" : "待确认"}</span></td>
        <td><div class="action-row"><button class="btn small primary" data-edit-source="${escapeHtml(row.key)}">${status === "owned" ? "调整归属" : "认领"}</button>
        ${status !== "ignored" ? `<button class="btn small" data-ignore="${escapeHtml(row.key)}">忽略</button>` : `<button class="btn small" data-restore="${escapeHtml(row.key)}">恢复待确认</button>`}</div></td></tr>`;
    }).join("") : `<tr><td colspan="8"><div class="empty"><strong>当前筛选没有作品</strong>可以切换状态或调整搜索条件</div></td></tr>`;
  }

  function renderDramas() {
    const query = text($("dramaSearch").value).toLowerCase();
    const filter = $("dramaFilter").value;
    const all = aggregateDramas();
    const rows = all.filter((drama) => {
      const haystack = `${drama.chinese_title} ${drama.english_title} ${drama.writer} ${drama.producer} ${drama.director} ${drama.cast} ${(drama.aliases || []).join(" ")} ${drama.accounts.join(" ")}`.toLowerCase();
      return (!query || haystack.includes(query)) && (filter === "all" || (filter === "live" ? drama.online : !drama.online));
    });
    $("dramaCount").textContent = `共 ${rows.length} 部 · ${all.filter((row) => row.online).length} 部已上架`;
    $("dramaBody").innerHTML = rows.length ? rows.map((drama, index) => `
      <tr><td><div class="action-row"><button class="btn small icon" data-move="up" data-drama="${escapeHtml(drama.id)}" title="上移" ${index === 0 ? "disabled" : ""}>↑</button>
      <button class="btn small icon" data-move="down" data-drama="${escapeHtml(drama.id)}" title="下移" ${index === rows.length - 1 ? "disabled" : ""}>↓</button></div></td>
      <td><div class="title-main">${escapeHtml(drama.chinese_title || "中文名待补充")}</div><div class="title-sub">${escapeHtml(drama.english_title || "英文名待补充")} · ${drama.sources.length} 条来源</div></td>
      <td>${drama.accounts.length ? drama.accounts.map((account) => `<span class="badge live" style="margin:2px">@${escapeHtml(account)}</span>`).join("") : '<span class="muted">尚未绑定</span>'}</td>
      <td>${escapeHtml(drama.writer || "—")}</td><td>${escapeHtml(drama.producer || "—")}</td><td class="metric">${formatNumber(drama.total_views)}</td>
      <td><button class="btn small" data-toggle-live="${escapeHtml(drama.id)}"><span class="badge ${drama.online ? "owned" : "pending"}">${drama.online ? "已上架" : "未上架"}</span></button></td>
      <td><div class="action-row"><button class="btn small primary" data-edit-drama="${escapeHtml(drama.id)}">编辑</button>
      <button class="btn small danger" data-delete-drama="${escapeHtml(drama.id)}">删除</button></div></td></tr>`).join("")
      : `<tr><td colspan="8"><div class="empty"><strong>还没有公司短剧</strong>请从作品认领中选择来源，或手动新建一部短剧</div></td></tr>`;
  }

  function renderAll() {
    renderStats();
    renderDashboard();
    renderAccounts();
    renderReview();
    renderDramas();
    updateStorageState();
  }

  function nextOrder() {
    return Math.max(0, ...Object.values(state.catalog.dramas).map((drama) => number(drama.order))) + 1;
  }

  function makeDramaId() {
    return `drama-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
  }

  function editorDramaOptions(selected = "__new__") {
    const options = aggregateDramas().map((drama) => `<option value="${escapeHtml(drama.id)}" ${drama.id === selected ? "selected" : ""}>${escapeHtml(drama.chinese_title || drama.english_title || drama.id)}</option>`).join("");
    $("editAttach").innerHTML = `<option value="__new__" ${selected === "__new__" ? "selected" : ""}>新建一部公司短剧</option>${options}`;
  }

  function fillEditor(drama, source) {
    $("editCn").value = drama?.chinese_title || source?.chinese_title || "";
    $("editEn").value = drama?.english_title || source?.english_title || "";
    $("editWriter").value = drama?.writer || "";
    $("editProducer").value = drama?.producer || "";
    $("editDirector").value = drama?.director || "";
    $("editCast").value = drama?.cast || "";
    $("editAliases").value = (drama?.aliases || []).join("\n");
    $("editOrder").value = number(drama?.order) || nextOrder();
    $("editOnline").checked = Boolean(drama?.online);
    $("editNotes").value = drama?.notes || "";
  }

  function openSourceEditor(keys) {
    if (!requireAuth("认领作品")) return;
    state.editorSourceKeys = [...new Set(keys)].filter((key) => state.sourceMap.has(key));
    state.editorDramaId = "";
    if (!state.editorSourceKeys.length) return;
    const sources = state.editorSourceKeys.map((key) => state.sourceMap.get(key));
    const existingIds = [...new Set(state.editorSourceKeys.map((key) => state.catalog.sources[key]?.drama_id).filter(Boolean))];
    const selected = existingIds.length === 1 ? existingIds[0] : "__new__";
    const drama = selected === "__new__" ? null : state.catalog.dramas[selected];
    editorDramaOptions(selected);
    fillEditor(drama, sources[0]);
    $("attachGroup").style.display = "grid";
    $("editTitle").textContent = state.editorSourceKeys.length > 1 ? `合并认领 ${state.editorSourceKeys.length} 条作品来源` : "认领公司短剧";
    $("editSubtitle").textContent = "可归入已有短剧，实现多账号同剧合并统计";
    $("editKey").value = "";
    $("sourceBox").innerHTML = sources.map((source) => `<div><strong>${escapeHtml(source.chinese_title || source.english_title)}</strong><br>@${escapeHtml(source.account)} · ${number(source.episodes)} 集 · ${formatNumber(source.views)}播放</div>`).join("");
    $("sourceBox").style.display = "grid";
    $("ignoreFromEdit").style.display = "inline-flex";
    $("saveDramaBtn").textContent = selected === "__new__" ? "保存并认领" : "保存并合并";
    $("editDialog").showModal();
  }

  function openDramaEditor(dramaId = "") {
    if (!requireAuth("编辑短剧")) return;
    state.editorSourceKeys = [];
    state.editorDramaId = dramaId;
    const drama = dramaId ? state.catalog.dramas[dramaId] : null;
    editorDramaOptions("__new__");
    fillEditor(drama, null);
    $("attachGroup").style.display = "none";
    $("editTitle").textContent = drama ? "编辑公司短剧" : "新建公司短剧";
    $("editSubtitle").textContent = drama ? `${dramaSources(dramaId).length} 条抓取来源已绑定` : "可先建立资料，之后再从作品认领中绑定来源";
    $("sourceBox").style.display = drama ? "grid" : "none";
    $("sourceBox").innerHTML = drama ? `<div><strong>当前来源</strong><br>${escapeHtml(dramaSources(dramaId).map((row) => `@${row.account}`).join("、") || "尚未绑定")}</div><div><strong>汇总播放</strong><br>${formatNumber(dramaSources(dramaId).reduce((sum, row) => sum + number(row.views), 0))}</div>` : "";
    $("ignoreFromEdit").style.display = "none";
    $("saveDramaBtn").textContent = "保存短剧资料";
    $("editDialog").showModal();
  }

  function dramaFromForm(existing = {}) {
    const aliases = $("editAliases").value.split(/\r?\n|,|\uFF0C/).map(text).filter(Boolean);
    return {
      ...existing,
      chinese_title: text($("editCn").value), english_title: text($("editEn").value),
      writer: text($("editWriter").value), producer: text($("editProducer").value),
      director: text($("editDirector").value), cast: text($("editCast").value), aliases: [...new Set(aliases)],
      order: Math.max(1, number($("editOrder").value) || nextOrder()), online: $("editOnline").checked,
      notes: text($("editNotes").value), created_at: existing.created_at || new Date().toISOString(), updated_at: new Date().toISOString(),
    };
  }

  async function saveEditor() {
    if (!$("editForm").reportValidity()) return;
    const sourceKeys = [...state.editorSourceKeys];
    const editingDramaId = state.editorDramaId;
    const attach = sourceKeys.length ? $("editAttach").value : "";
    const saved = await mutateAndSave(() => {
      const dramaId = editingDramaId || (attach !== "__new__" ? attach : makeDramaId());
      const existing = state.catalog.dramas[dramaId] || {};
      state.catalog.dramas[dramaId] = { id: dramaId, ...dramaFromForm(existing) };
      for (const key of sourceKeys) {
        state.catalog.sources[key] = { status: "owned", drama_id: dramaId, updated_at: new Date().toISOString() };
      }
    }, sourceKeys.length > 1 ? `已合并 ${sourceKeys.length} 条来源并保存` : "短剧资料已保存到后台");
    if (saved) $("editDialog").close();
  }

  function selectedReviewKeys() {
    return [...document.querySelectorAll(".review-check:checked")].map((node) => node.dataset.key);
  }

  async function ignoreSources(keys) {
    const clean = [...new Set(keys)].filter(Boolean);
    if (!clean.length) return toast("请先选择作品", true);
    await mutateAndSave(() => {
      for (const key of clean) state.catalog.sources[key] = { status: "ignored", drama_id: "", updated_at: new Date().toISOString() };
    }, `已忽略 ${clean.length} 条作品来源`);
  }

  async function restoreSource(key) {
    await mutateAndSave(() => {
      state.catalog.sources[key] = { status: "pending", drama_id: "", updated_at: new Date().toISOString() };
    }, "已恢复为待确认");
  }

  async function toggleLive(dramaId) {
    await mutateAndSave(() => {
      const drama = state.catalog.dramas[dramaId];
      if (drama) { drama.online = !drama.online; drama.updated_at = new Date().toISOString(); }
    }, state.catalog.dramas[dramaId]?.online ? "已下架，不再出现在公开剧库" : "已上架到公开公司剧库");
  }

  async function moveDrama(dramaId, direction) {
    const dramas = aggregateDramas();
    const index = dramas.findIndex((drama) => drama.id === dramaId);
    const target = direction === "up" ? index - 1 : index + 1;
    if (index < 0 || target < 0 || target >= dramas.length) return;
    await mutateAndSave(() => {
      dramas.forEach((drama, position) => { state.catalog.dramas[drama.id].order = position + 1; });
      const currentOrder = state.catalog.dramas[dramaId].order;
      state.catalog.dramas[dramaId].order = state.catalog.dramas[dramas[target].id].order;
      state.catalog.dramas[dramas[target].id].order = currentOrder;
    }, "前台展示顺序已更新");
  }

  async function deleteDrama(dramaId) {
    const drama = state.catalog.dramas[dramaId];
    if (!drama) return;
    if (!confirm(`确定删除“${drama.chinese_title || drama.english_title}”吗？已绑定来源会恢复为待确认。`)) return;
    await mutateAndSave(() => {
      delete state.catalog.dramas[dramaId];
      for (const relation of Object.values(state.catalog.sources)) {
        if (relation.drama_id === dramaId) { relation.status = "pending"; relation.drama_id = ""; relation.updated_at = new Date().toISOString(); }
      }
    }, "短剧已删除，来源已恢复为待确认");
  }

  function exportCatalog() {
    const blob = new Blob([JSON.stringify(state.catalog, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `paqu-admin-catalog-r${state.catalog.revision}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function addAccounts() {
    if (!requireAuth("添加账号")) return;
    const raw = text($("newAccounts").value);
    if (!raw) return toast("请输入至少一个 TikTok 账号或主页链接", true);
    try {
      const payload = await api("/discover-accounts", {
        method: "POST", headers: authHeaders(true), body: JSON.stringify({ accounts: raw }),
      });
      state.backendAccounts = payload.accounts || [];
      $("newAccounts").value = "";
      $("accountDialog").close();
      renderAll();
      toast(payload.added_count ? `已新增 ${payload.added_count} 个监控账号` : "输入账号均已存在，无需重复添加");
    } catch (error) {
      toast(error.message, true);
    }
  }

  document.querySelectorAll(".nav-btn[data-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  document.querySelectorAll("[data-jump]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.jump)));
  $("menuBtn").addEventListener("click", () => $("sidebar").classList.toggle("open"));
  $("noticeClose").addEventListener("click", () => $("draftNotice").remove());
  $("authBtn").addEventListener("click", () => $("authBox").classList.toggle("open"));
  $("verifyBtn").addEventListener("click", async () => {
    const value = text($("secretInput").value);
    if (!value) return toast("请输入后台任务密码", true);
    sessionStorage.setItem(SESSION_KEY, value);
    await loadAdminCatalog(true);
  });
  $("secretInput").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); $("verifyBtn").click(); } });
  $("refreshBtn").addEventListener("click", () => state.verified ? loadAdminCatalog(true) : loadPublicReport());
  $("reloadCatalogBtn").addEventListener("click", () => state.verified ? loadAdminCatalog(true) : requireAuth("读取后台数据"));
  $("loadBackendAccountsBtn").addEventListener("click", () => loadBackendAccounts());
  $("addAccountBtn").addEventListener("click", () => { if (requireAuth("添加账号")) $("accountDialog").showModal(); });
  $("saveAccountsBtn").addEventListener("click", (event) => { event.preventDefault(); addAccounts(); });
  $("newDramaBtn").addEventListener("click", () => openDramaEditor());
  $("newDramaBtn2").addEventListener("click", () => openDramaEditor());
  $("saveDramaBtn").addEventListener("click", (event) => { event.preventDefault(); saveEditor(); });
  $("ignoreFromEdit").addEventListener("click", async () => { await ignoreSources(state.editorSourceKeys); $("editDialog").close(); });
  $("editAttach").addEventListener("change", () => {
    const value = $("editAttach").value;
    const source = state.editorSourceKeys.length ? state.sourceMap.get(state.editorSourceKeys[0]) : null;
    fillEditor(value === "__new__" ? null : state.catalog.dramas[value], source);
    $("saveDramaBtn").textContent = value === "__new__" ? "保存并认领" : "保存并合并";
  });
  $("reviewSearch").addEventListener("input", () => { state.reviewPage = 1; renderReview(); });
  $("reviewFilter").addEventListener("change", () => { state.reviewPage = 1; renderReview(); });
  $("reviewSort").addEventListener("change", () => { state.reviewPage = 1; renderReview(); });
  $("reviewPrev").addEventListener("click", () => { state.reviewPage--; renderReview(); });
  $("reviewNext").addEventListener("click", () => { state.reviewPage++; renderReview(); });
  $("reviewCheckAll").addEventListener("change", (event) => document.querySelectorAll(".review-check").forEach((node) => { node.checked = event.target.checked; }));
  $("bulkClaimBtn").addEventListener("click", () => {
    const keys = selectedReviewKeys();
    if (!keys.length) return toast("请先勾选要认领的作品", true);
    openSourceEditor(keys);
  });
  $("bulkIgnoreBtn").addEventListener("click", () => ignoreSources(selectedReviewKeys()));
  $("accountSearch").addEventListener("input", renderAccounts);
  $("accountFilter").addEventListener("change", renderAccounts);
  $("dramaSearch").addEventListener("input", renderDramas);
  $("dramaFilter").addEventListener("change", renderDramas);
  $("exportDraftBtn").addEventListener("click", exportCatalog);
  $("settingsExportBtn").addEventListener("click", exportCatalog);

  document.addEventListener("click", (event) => {
    const sourceButton = event.target.closest("[data-edit-source]");
    if (sourceButton) return openSourceEditor([sourceButton.dataset.editSource]);
    const dramaButton = event.target.closest("[data-edit-drama]");
    if (dramaButton) return openDramaEditor(dramaButton.dataset.editDrama);
    const ignoreButton = event.target.closest("[data-ignore]");
    if (ignoreButton) return ignoreSources([ignoreButton.dataset.ignore]);
    const restoreButton = event.target.closest("[data-restore]");
    if (restoreButton) return restoreSource(restoreButton.dataset.restore);
    const liveButton = event.target.closest("[data-toggle-live]");
    if (liveButton) return toggleLive(liveButton.dataset.toggleLive);
    const moveButton = event.target.closest("[data-move]");
    if (moveButton) return moveDrama(moveButton.dataset.drama, moveButton.dataset.move);
    const deleteButton = event.target.closest("[data-delete-drama]");
    if (deleteButton) return deleteDrama(deleteButton.dataset.deleteDrama);
  });

  renderAll();
  loadPublicReport().then(() => {
    if (secret()) {
      $("secretInput").value = secret();
      loadAdminCatalog(false);
    }
  });
})();
