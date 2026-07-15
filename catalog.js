(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const number = (value) => Number(value || 0);
  const text = (value) => String(value ?? "").trim();
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const state = { dramas: [], ranking: [], generatedAt: "", updatedAt: "" };

  function formatNumber(value) {
    const n = number(value);
    if (n >= 100000000) return `${(n / 100000000).toFixed(n >= 1000000000 ? 1 : 2).replace(/\.0+$/, "")}亿`;
    if (n >= 10000) return `${(n / 10000).toFixed(n >= 100000 ? 1 : 2).replace(/\.0+$/, "")}万`;
    return n.toLocaleString("zh-CN");
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(String(value).replace(" ", "T"));
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
  }

  function renderHero() {
    const accounts = new Set(state.dramas.flatMap((drama) => drama.accounts || []));
    $("heroCount").textContent = state.dramas.length;
    $("heroViews").textContent = formatNumber(state.dramas.reduce((sum, drama) => sum + number(drama.total_views), 0));
    $("heroAccounts").textContent = accounts.size;
    $("updatedText").textContent = `抓取数据：${formatTime(state.generatedAt)} · 资料更新：${formatTime(state.updatedAt)}`;
  }

  function renderRanking() {
    const rows = state.ranking;
    const podium = rows.slice(0, 3);
    $("rankingCount").textContent = rows.length ? `共 ${rows.length} 部上榜` : "暂无上榜作品";
    $("ranking").innerHTML = podium.length ? podium.map((drama, index) => `
      <article class="rank-card"><div class="rank-number">${index + 1}</div><div class="rank-label">TOP ${index + 1}</div>
      <div class="rank-title">${escapeHtml(drama.chinese_title || drama.english_title || "未命名短剧")}</div>
      <div class="rank-en">${escapeHtml(drama.english_title || "英文名待补充")}</div>
      <div class="rank-bottom"><div class="rank-views">${formatNumber(drama.total_views)}</div><div class="rank-source">${number(drama.source_count)} 条来源 · ${number(drama.episodes)} 集</div></div><button class="rank-open" data-detail="${escapeHtml(drama.id)}">查看详情 →</button></article>`).join("")
      : `<div class="empty"><strong>暂无公开榜单</strong>请在管理后台认领公司作品并设置为上架</div>`;
    $("rankingTableBody").innerHTML = rows.length ? rows.map((drama, index) => `
      <tr><td><span class="rank-chip">${index + 1}</span></td>
      <td class="rank-drama"><strong>${escapeHtml(drama.chinese_title || drama.english_title || "未命名短剧")}</strong><small>${escapeHtml(drama.english_title || "英文名待补充")}</small></td>
      <td class="rank-accounts">${(drama.accounts || []).length ? drama.accounts.map((account) => `@${escapeHtml(account)}`).join("、") : "—"}</td>
      <td class="rank-people">${escapeHtml(drama.writer || "待补充")}</td><td class="rank-people">${escapeHtml(drama.producer || "待补充")}</td>
      <td class="rank-play">${formatNumber(drama.total_views)}</td><td><button class="rank-open" data-detail="${escapeHtml(drama.id)}">打开 →</button></td></tr>`).join("")
      : `<tr><td class="leaderboard-empty" colspan="7"><strong>暂无已上架的公司短剧</strong>管理员认领作品并开启“前台展示”后，榜单会自动生成</td></tr>`;
  }

  function filteredDramas() {
    const query = text($("search").value).toLowerCase();
    const sort = $("sort").value;
    const rows = state.dramas.filter((drama) => `${drama.chinese_title} ${drama.english_title} ${drama.writer} ${drama.producer} ${drama.director} ${drama.cast} ${(drama.aliases || []).join(" ")} ${(drama.accounts || []).join(" ")}`.toLowerCase().includes(query));
    rows.sort(sort === "views" ? (a, b) => number(b.total_views) - number(a.total_views)
      : sort === "latest" ? (a, b) => text(b.latest_publish_time).localeCompare(text(a.latest_publish_time))
        : sort === "title" ? (a, b) => text(a.chinese_title || a.english_title).localeCompare(text(b.chinese_title || b.english_title), "zh-CN")
          : (a, b) => number(a.order || 999999) - number(b.order || 999999));
    return rows;
  }

  function renderCatalog() {
    const rows = filteredDramas();
    $("resultCount").textContent = `显示 ${rows.length} / ${state.dramas.length} 部`;
    $("catalogGrid").classList.remove("loading");
    $("catalogGrid").innerHTML = rows.length ? rows.map((drama) => `
      <article class="drama"><div class="drama-top"><span class="drama-rank">热度 #${number(drama.rank) || "—"}</span>
      <h3>${escapeHtml(drama.chinese_title || drama.english_title || "未命名短剧")}</h3><div class="english">${escapeHtml(drama.english_title || "英文名待补充")}</div></div>
      <div class="drama-body"><div class="people"><div class="meta">编剧<strong>${escapeHtml(drama.writer || "待补充")}</strong></div><div class="meta">制作/制片<strong>${escapeHtml(drama.producer || "待补充")}</strong></div>
      <div class="meta">导演<strong>${escapeHtml(drama.director || "—")}</strong></div><div class="meta">主演<strong>${escapeHtml(drama.cast || "—")}</strong></div></div>
      <div><div class="meta" style="margin-bottom:6px">发布账号</div><div class="accounts">${(drama.accounts || []).length ? drama.accounts.map((account) => `<span class="tag">@${escapeHtml(account)}</span>`).join("") : '<span class="tag">暂无绑定</span>'}</div></div></div>
      <div class="drama-foot"><div class="views"><strong>${formatNumber(drama.total_views)}</strong><span>${number(drama.episodes)} 集 · ${number(drama.source_count)} 条合并来源</span></div>
      <button class="detail-btn" data-detail="${escapeHtml(drama.id)}">查看详情 →</button></div></article>`).join("")
      : `<div class="empty"><strong>${state.dramas.length ? "没有匹配的短剧" : "暂无已上架公司短剧"}</strong>${state.dramas.length ? "请调整搜索条件" : "运营人员可在管理后台认领作品并上架"}</div>`;
  }

  function openDetail(id) {
    const drama = state.dramas.find((row) => row.id === id);
    if (!drama) return;
    $("detailTitle").textContent = drama.chinese_title || drama.english_title || "短剧详情";
    $("detailEnglish").textContent = drama.english_title || "英文名待补充";
    $("detailStats").innerHTML = `<div class="detail-stat"><strong>${formatNumber(drama.total_views)}</strong><span>累计播放</span></div><div class="detail-stat"><strong>${number(drama.episodes)}</strong><span>剧集数</span></div><div class="detail-stat"><strong>${number(drama.source_count)}</strong><span>合并发布来源</span></div>`;
    $("detailPeople").innerHTML = `<div class="meta">编剧<strong>${escapeHtml(drama.writer || "待补充")}</strong></div><div class="meta">制作/制片<strong>${escapeHtml(drama.producer || "待补充")}</strong></div><div class="meta">导演<strong>${escapeHtml(drama.director || "—")}</strong></div><div class="meta">主演<strong>${escapeHtml(drama.cast || "—")}</strong></div>`;
    $("detailSources").innerHTML = (drama.sources || []).length ? drama.sources.map((source) => `
      <div class="source-row"><div><strong>${escapeHtml(source.nickname || source.account || "发布账号")}</strong><small>@${escapeHtml(source.account)} · ${formatTime(source.publish_time)}</small></div>
      <div><strong>${formatNumber(source.views)}</strong><small>${number(source.episodes)} 集</small></div>
      ${source.profile_url ? `<a class="source-link" href="${escapeHtml(source.profile_url)}" target="_blank" rel="noopener">账号主页 ↗</a>` : ""}</div>`).join("")
      : `<div class="empty" style="padding:30px"><strong>来源暂不可用</strong>等待下一次抓取同步</div>`;
    $("detailDialog").showModal();
  }

  async function load() {
    try {
      const response = await fetch(`/curated-catalog?t=${Date.now()}`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "读取失败");
      state.dramas = Array.isArray(payload.dramas) ? payload.dramas : [];
      state.ranking = Array.isArray(payload.ranking) ? payload.ranking : [];
      const rankMap = new Map(state.ranking.map((drama, index) => [drama.id, index + 1]));
      state.dramas.forEach((drama) => { drama.rank = rankMap.get(drama.id) || 0; });
      state.generatedAt = payload.generated_at || "";
      state.updatedAt = payload.updated_at || "";
      renderHero(); renderRanking(); renderCatalog();
    } catch (error) {
      $("updatedText").textContent = "资料读取失败，请稍后刷新";
      $("catalogGrid").classList.remove("loading");
      $("catalogGrid").innerHTML = `<div class="empty"><strong>公司短剧资料暂时无法读取</strong>${escapeHtml(error.message)}</div>`;
    }
  }

  $("search").addEventListener("input", renderCatalog);
  $("sort").addEventListener("change", renderCatalog);
  $("resetBtn").addEventListener("click", () => { $("search").value = ""; $("sort").value = "manual"; renderCatalog(); });
  ["ranking", "rankingTableBody", "catalogGrid"].forEach((id) => $(id).addEventListener("click", (event) => { const button = event.target.closest("[data-detail]"); if (button) openDetail(button.dataset.detail); }));
  $("closeDialog").addEventListener("click", () => $("detailDialog").close());
  load();
})();
