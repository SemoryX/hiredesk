/* AI 求职工作台 —— 前端逻辑（原生 JS，无构建步骤） */
(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC_MAP[c]);
  const dash = (v) => (v === null || v === undefined || v === "" ? "—" : v);

  const state = {
    view: "overview",
    bootstrap: null,
    jobs: { limit: 50, offset: 0, total: 0 },
    pollTimer: null,
    currentJob: null,
    selected: new Set(),
  };

  /* ---------------- 基础工具 ---------------- */
  function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.toggle("err", !!isError);
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), 2600);
  }

  async function api(path, { method = "GET", body = null } = {}) {
    const opts = { method, headers: {} };
    if (body !== null) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(path, opts);
    } catch {
      // 服务刚重启、或页面开着但后端没起来时，fetch 会直接抛错，原始信息只有 "Failed to fetch"
      throw new Error("连不上本地服务（可能刚重启），请刷新页面后重试");
    }
    let data = {};
    try { data = await resp.json(); } catch { /* 空响应 */ }
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  }

  function money(job) {
    if (job.salary_raw) return job.salary_raw;
    if (job.salary_min != null && job.salary_max != null) {
      const mo = job.salary_months ? `·${job.salary_months}薪` : "";
      return `${trimNum(job.salary_min)}-${trimNum(job.salary_max)}K${mo}`;
    }
    if (job.salary_min != null) return `${trimNum(job.salary_min)}K+`;
    return "—";
  }
  const trimNum = (n) => (Number.isInteger(Number(n)) ? String(Number(n)) : String(n));

  const FIT_LABEL = { high: "高", medium: "中", low: "低" };
  const STATUS_LABEL = { new: "待处理", want: "想投递", applied: "已投递",
                         skipped: "已跳过", expired: "已过期" };
  const PORTAL_LABEL = {
    boss: "BOSS", liepin: "猎聘", "51job": "前程无忧", zhilian: "智联",
  };

  function portalLabel(site) {
    if (!site) return "—";
    const key = String(site).toLowerCase();
    return PORTAL_LABEL[key] || String(site);
  }

  function portalClass(site) {
    const key = String(site || "").toLowerCase();
    if (key === "boss") return "portal-boss";
    if (key === "liepin") return "portal-liepin";
    if (key === "51job") return "portal-51job";
    if (key === "zhilian") return "portal-zhilian";
    return "none";
  }

  function portalCell(job) {
    const list = Array.isArray(job.sources) ? job.sources : [];
    const sites = [...new Set(list.map((s) => s && s.site).filter(Boolean))];
    const primary = job.portal || job.src_site || sites[0] || "";
    if (!primary && !sites.length) return `<span class="muted">—</span>`;
    const extra = sites.filter((s) => s !== primary);
    const more = extra.length
      ? ` <span class="muted" title="${esc(extra.map(portalLabel).join('、'))}">+${extra.length}</span>`
      : "";
    return `<span class="pill ${portalClass(primary)}" title="${esc(primary)}">${esc(portalLabel(primary))}</span>${more}`;
  }

  function fitPill(fit) {
    if (!fit) return `<span class="pill none">未评</span>`;
    return `<span class="pill ${esc(fit)}">${FIT_LABEL[fit] || esc(fit)}</span>`;
  }
  function statusPill(status) {
    return `<span class="pill status-${esc(status)}">${STATUS_LABEL[status] || esc(status)}</span>`;
  }
  function scoreBadge(job) {
    if (job.score == null) return `<span class="scorebadge" style="background:#20262f;color:var(--muted)">—</span>`;
    const color = job.fit === "high" ? "#12331f;color:#6ee7a6"
      : job.fit === "medium" ? "#332a12;color:#e8c46a" : "#331b1b;color:#f0a19a";
    return `<span class="scorebadge" style="background:${color}">${job.score}</span>`;
  }

  /* ---------------- 视图切换 ---------------- */
  const VIEWS = ["overview", "jobs", "studio", "collect", "applications", "profile"];

  function viewFromHash() {
    const hash = location.hash.replace(/^#/, "");
    return VIEWS.includes(hash) ? hash : "overview";
  }

  function switchView(view) {
    state.view = view;
    if (location.hash.slice(1) !== view) history.replaceState(null, "", `#${view}`);
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
    if (view === "overview") loadOverview();
    if (view === "jobs") loadJobs();
    if (view === "studio") loadResumeSources();
    if (view === "collect") {
      loadRuns(); pollScrape(); applyCollectDefaults();
      loadLoginState(true);
      startLoginAutoRefresh();
    } else {
      stopLoginAutoRefresh();
    }
    if (view === "applications") loadApplications();
    if (view === "profile") { fillProfile(state.bootstrap?.profile || {}); loadDocuments(); }
  }

  /* ---------------- 启动 ---------------- */
  async function loadBootstrap() {
    const data = await api("/api/bootstrap");
    state.bootstrap = data;
    setEngineBadge(data.engine);
    $("#badgeDb").classList.add("ok");
    $("#badgeDb").querySelector("span").textContent = data.db;

    // 筛选下拉
    const citySel = $("#fCity");
    citySel.innerHTML = `<option value="">全部城市</option>` +
      (data.facets.cities || []).map((c) => `<option value="${esc(c.city)}">${esc(c.city)} (${c.n})</option>`).join("");
    const siteSel = $("#fSite");
    siteSel.innerHTML = `<option value="">全部站点</option>` +
      (data.facets.sites || []).map((s) => `<option value="${esc(s.portal)}">${esc(s.portal)} (${s.n})</option>`).join("");

    const script = $("#engineScript");
    if (script) script.textContent = data.engine?.script || "jobscraper.py";
    fillProfile(data.profile || {});
    applyCollectDefaults();
    // 关键词/城市输入框给出画像里的候选项，可直接点选
    const splitList = (text) => String(text || "").split(/[,，;；、/|]+/).map((s) => s.trim()).filter(Boolean);
    const roles = splitList(data.profile?.target_roles);
    const cities = splitList(data.profile?.preferred_cities);
    const loco = (data.profile?.location || "").trim();
    if (loco && !cities.includes(loco)) cities.unshift(loco);
    $("#cKeywordList").innerHTML = roles.map((r) => `<option value="${esc(r)}"></option>`).join("");
    $("#cCityList").innerHTML = cities.map((c) => `<option value="${esc(c)}"></option>`).join("");
  }

  function setEngineBadge(engine) {
    const el = $("#badgeUpstream");
    const ok = !!engine?.ok;
    el.classList.toggle("ok", ok);
    el.classList.toggle("bad", !ok);
    el.querySelector("span").textContent = ok ? "采集引擎就绪" : "采集引擎不可用";
  }

  async function refreshAll() {
    try {
      await loadBootstrap();
      if (state.view === "overview") await loadOverview();
      if (state.view === "jobs") await loadJobs();
      if (state.view === "applications") await loadApplications();
      if (state.view === "collect") await loadRuns();
      toast("已刷新");
    } catch (err) { toast(err.message, true); }
  }

  /* ---------------- 概览 ---------------- */
  async function loadOverview() {
    let data;
    try { data = await api("/api/overview"); }
    catch (err) { toast(err.message, true); return; }
    setEngineBadge(data.engine);
    const k = data.kpis;
    $("#kpis").innerHTML = [
      ["岗位总数", k.total, ""],
      ["待处理", k.new, "blue"],
      ["想投递", k.want ?? 0, "accent"],
      ["高匹配", k.high_fit, "accent"],
      ["已投递", k.applied, ""],
      ["申请记录", k.applications, ""],
      ["公司数", k.companies, ""],
      ["覆盖城市", k.cities, ""],
      ["平均月薪", k.avg_salary != null ? `${k.avg_salary}K` : "—", "accent"],
    ].map(([label, value, cls]) =>
      `<div class="kpi"><div class="k">${label}</div><div class="v ${cls}">${value}</div></div>`
    ).join("");

    // 薪资分布
    const buckets = data.salary_buckets || [];
    const maxSalary = Math.max(1, ...buckets.map((b) => b.n));
    $("#salaryChart").innerHTML = buckets.filter((b) => b.bucket !== "未知" || b.n > 0).map((b) => `
      <div class="barrow">
        <div class="lbl">${esc(b.bucket)}</div>
        <div class="bartrack"><div class="barfill" style="width:${(b.n / maxSalary) * 100}%"></div></div>
        <div class="num">${b.n}</div>
      </div>`).join("");

    // 城市
    const cities = data.top_cities || [];
    const maxCity = Math.max(1, ...cities.map((c) => c.n));
    $("#cityChart").innerHTML = cities.length ? cities.map((c) => `
      <div class="barrow">
        <div class="lbl">${esc(c.city)}</div>
        <div class="bartrack"><div class="barfill blue" style="width:${(c.n / maxCity) * 100}%"></div></div>
        <div class="num">${c.n}</div>
      </div>`).join("") : `<div class="empty">还没有数据，去「采集中心」抓一批</div>`;

    // 匹配度
    const fits = data.by_fit || {};
    const fitTotal = Math.max(1, Object.values(fits).reduce((a, b) => a + b, 0));
    $("#fitChart").innerHTML = ["high", "medium", "low", "none"].map((key) => {
      const n = fits[key] || 0;
      return `<div class="fitrow">
        <div>${key === "none" ? `<span class="pill none">未评</span>` : fitPill(key)}</div>
        <div class="bartrack"><div class="barfill ${key === "low" ? "" : "blue"}" style="width:${(n / fitTotal) * 100}%"></div></div>
        <div class="num">${n}</div>
      </div>`;
    }).join("");

    // 最近入库
    const recent = data.recent || [];
    $("#recentList").innerHTML = recent.length ? recent.map((j) => `
      <div class="item" data-key="${esc(j.job_key)}">
        <div class="t"><b>${esc(j.title)}</b> <span class="muted">· ${esc(j.company || "—")} · ${esc(j.city || "—")}</span></div>
        <div>${scoreBadge(j)}</div>
      </div>`).join("") : `<div class="empty">岗位池是空的</div>`;
    $$("#recentList .item").forEach((el) => el.addEventListener("click", () => openJob(el.dataset.key)));
  }

  /* ---------------- 岗位池 ---------------- */
  async function loadJobs() {
    const params = new URLSearchParams({
      q: $("#fQ").value.trim(),
      city: $("#fCity").value,
      site: $("#fSite").value,
      fit: $("#fFit").value,
      status: $("#fStatus").value,
      sort: $("#fSort").value,
      limit: state.jobs.limit,
      offset: state.jobs.offset,
    });
    let data;
    try { data = await api(`/api/jobs?${params}`); }
    catch (err) { toast(err.message, true); return; }
    state.jobs.total = data.total;

    $("#jobsBody").innerHTML = data.rows.length ? data.rows.map((j) => {
      const picked = state.selected.has(j.job_key);
      return `
      <tr data-key="${esc(j.job_key)}" class="${picked ? "selected" : ""}">
        <td class="c-check"><input type="checkbox" class="row-check" data-key="${esc(j.job_key)}"${picked ? " checked" : ""} /></td>
        <td class="c-title" title="${esc(j.title)}">${esc(j.title)}</td>
        <td>${portalCell(j)}</td>
        <td>${esc(dash(j.company))}</td>
        <td>${esc(dash(j.city))}${j.district ? `<span class="muted">·${esc(j.district)}</span>` : ""}</td>
        <td class="num">${esc(money(j))}</td>
        <td>${esc(dash(j.experience))}</td>
        <td class="c-score">${scoreBadge(j)}</td>
        <td>${statusPill(j.status)}</td>
      </tr>`;
    }).join("")
      : `<tr><td colspan="9"><div class="empty">没有符合条件的岗位</div></td></tr>`;

    $$("#jobsBody tr[data-key]").forEach((tr) =>
      tr.addEventListener("click", (e) => {
        if (e.target.matches("input")) return;   // 点复选框不该开详情
        openJob(tr.dataset.key);
      }));
    $$("#jobsBody .row-check").forEach((box) => {
      box.addEventListener("click", (e) => e.stopPropagation());
      box.addEventListener("change", () => {
        if (box.checked) state.selected.add(box.dataset.key);
        else state.selected.delete(box.dataset.key);
        box.closest("tr").classList.toggle("selected", box.checked);
        syncSelection();
      });
    });

    const from = data.total ? state.jobs.offset + 1 : 0;
    const to = Math.min(state.jobs.offset + state.jobs.limit, data.total);
    $("#jobsCount").textContent = `共 ${data.total} 条，当前 ${from}-${to}`;
    $("#btnPrev").disabled = state.jobs.offset <= 0;
    $("#btnNext").disabled = state.jobs.offset + state.jobs.limit >= data.total;
    syncSelection();
  }

  function syncSelection() {
    const count = state.selected.size;
    const btn = $("#btnBatchDelete");
    btn.textContent = count ? `删除选中 (${count})` : "删除选中";
    btn.disabled = count === 0;
    const boxes = $$("#jobsBody .row-check");
    const all = $("#checkAll");
    all.checked = boxes.length > 0 && boxes.every((b) => b.checked);
    all.indeterminate = !all.checked && boxes.some((b) => b.checked);
  }

  /* ---------------- 岗位详情抽屉 ---------------- */
  async function openJob(key) {
    let job;
    try { job = await api(`/api/jobs/${encodeURIComponent(key)}`); }
    catch (err) { toast(err.message, true); return; }
    state.currentJob = job;

    $("#dTitle").textContent = job.title;
    $("#dCompany").textContent = [job.company, job.city, job.portal].filter(Boolean).join(" · ");

    const bd = job.breakdown || {};
    const axes = [["technical", "技术"], ["experience", "经验"], ["behavioral", "软性"], ["career", "职业"]];
    const breakdownHtml = axes.map(([k, label]) => {
      const val = bd[k] ?? 0;
      return `<div class="bdrow">
        <div class="muted">${label}</div>
        <div class="bartrack"><div class="barfill ${val >= 60 ? "" : "blue"}" style="width:${val}%"></div></div>
        <div class="num">${Math.round(val)}</div>
      </div>`;
    }).join("");

    const list = (arr) => (arr && arr.length)
      ? `<ul class="list">${arr.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<div class="muted">无</div>`;

    const sourcesHtml = (() => {
      const list2 = Array.isArray(job.sources) ? job.sources : [];
      if (!list2.length) {
        return `<div class="muted">${esc(job.src_site || job.portal || "—")} ${job.src_job_id ? `· ${esc(job.src_job_id)}` : ""}</div>`;
      }
      return `<ul class="list">${list2.map((s) => {
        const label = `${esc(s.site || "?")}${s.job_id ? ` · ${esc(s.job_id)}` : ""}`;
        return `<li>${s.url
          ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${label}</a>`
          : label}</li>`;
      }).join("")}</ul>`;
    })();

    const companyMeta = (() => {
      const m = job.company_meta;
      if (!m || typeof m !== "object") return "";
      return Object.values(m).filter(Boolean).map((v) => esc(v)).join(" · ");
    })();

    $("#dBody").innerHTML = `
      <div class="kv">
        <div class="k">薪资</div><div>${esc(money(job))}</div>
        <div class="k">经验 / 学历</div><div>${esc(dash(job.experience))} / ${esc(dash(job.education))}</div>
        <div class="k">匹配分</div><div>${job.score != null ? `<b>${job.score}</b> ${fitPill(job.fit)}` : "未评分"}</div>
        <div class="k">状态</div><div>${statusPill(job.status)}</div>
        <div class="k">技能</div><div>${esc(dash(job.skills))}</div>
        <div class="k">公司地点</div><div>${esc(dash(job.company_address))}</div>
        <div class="k">公司信息</div><div>${esc(companyMeta || dash(null))}</div>
        <div class="k">入库时间</div><div>${esc(dash(job.first_seen))}</div>
        <div class="k">发布来源</div><div>${sourcesHtml}</div>
      </div>
      ${job.breakdown ? `<div class="section-title">匹配度拆解</div><div class="breakdown">${breakdownHtml}</div>` : ""}
      ${job.description ? `<div class="section-title">岗位职责</div><div class="jd">${esc(job.description)}</div>` : ""}
      ${job.company_intro ? `<div class="section-title">公司介绍</div><div class="jd">${esc(job.company_intro)}</div>` : ""}
      <div class="section-title">加分项</div>${list(job.strengths)}
      <div class="section-title">待确认 / 差距</div>${list(job.gaps)}
      <details><summary>原始数据（jobscraper 接口返回）</summary><pre class="raw">${esc(pretty(job.raw))}</pre></details>
    `;

    $("#dFoot").innerHTML = `
      <button class="btn primary" id="dScore">重新评分</button>
      <button class="btn" id="dWant">想投递</button>
      <button class="btn" id="dApply">标记已投递</button>
      <button class="btn" id="dStudio">按此 JD 优化简历</button>
      <button class="btn" id="dSkip">跳过</button>
      <button class="btn ghost" id="dRestore">恢复待处理</button>
      <button class="btn danger" id="dDelete">删除岗位</button>
      ${job.url ? `<button class="btn primary" id="dOpenFill">打开原网页</button>` : ""}
    `;
    $("#dScore").onclick = () => runJobAction(key, "score");
    $("#dApply").onclick = () => runJobAction(key, "apply");
    if ($("#dWant")) {
      $("#dWant").onclick = () => runJobAction(key, "status", { status: "want" });
    }
    $("#dSkip").onclick = () => runJobAction(key, "status", { status: "skipped" });
    $("#dRestore").onclick = () => runJobAction(key, "status", { status: "new" });
    $("#dStudio").onclick = () => openStudioForJob(job);
    if ($("#dOpenFill")) {
      $("#dOpenFill").onclick = async () => {
        const btn = $("#dOpenFill");
        btn.disabled = true;
        btn.textContent = "正在打开…";
        try {
          await api(`/api/jobs/${encodeURIComponent(key)}/open-fill`, {
            method: "POST",
            body: { site: job.src_site || "boss" },
          });
          toast("已打开原网页；若出现滑块请拖一下，完成后会自动补全");
          btn.textContent = "补全中…";
          // 打开+补全大约 5~10 秒，之后重新拉这条岗位并重绘抽屉
          setTimeout(async () => {
            try {
              const fresh = await api(`/api/jobs/${encodeURIComponent(key)}`);
              await openJob(key);
              const jd = (fresh.description || "").length;
              toast(jd > 150 ? `岗位信息已补全（JD ${jd} 字）` : "已打开，但这次没抓到详情");
            } catch { /* 刷新失败不影响使用 */ }
            loadJobs();
            btn.disabled = false;
            btn.textContent = "打开原网页";
          }, 9000);
        } catch (err) {
          toast(err.message, true);
          btn.disabled = false;
          btn.textContent = "打开原网页";
        }
      };
    }
    $("#dDelete").onclick = async () => {
      if (!confirm(`删除岗位「${job.title}」？此操作不可恢复。`)) return;
      try {
        const r = await api(`/api/jobs/${encodeURIComponent(key)}/delete`, { method: "POST" });
        closeDrawer();
        toast(r.applications_kept ? `已删除（保留了 ${r.applications_kept} 条申请记录）` : "已删除岗位");
        loadJobs();
      } catch (err) { toast(err.message, true); }
    };

    $("#drawer").classList.add("open");
    $("#drawerMask").classList.add("open");
  }

  function pretty(raw) {
    if (!raw) return "（无）";
    try {
      const obj = typeof raw === "string" ? JSON.parse(raw) : raw;
      return JSON.stringify(obj, null, 2);
    } catch { return String(raw).slice(0, 4000); }
  }

  function closeDrawer() {
    $("#drawer").classList.remove("open");
    $("#drawerMask").classList.remove("open");
    state.currentJob = null;
  }

  async function runJobAction(key, action, body) {
    try {
      if (action === "score") {
        const r = await api(`/api/jobs/${encodeURIComponent(key)}/score`, { method: "POST" });
        toast(`已重新评分：${r.score}（${FIT_LABEL[r.fit] || r.fit}）`);
        await openJob(key);
      } else if (action === "apply") {
        await api(`/api/jobs/${encodeURIComponent(key)}/apply`, { method: "POST" });
        toast("已加入申请追踪（草稿）");
        await openJob(key);
      } else {
        await api(`/api/jobs/${encodeURIComponent(key)}/status`, { method: "POST", body });
        toast(`状态已更新：${STATUS_LABEL[body.status] || body.status}`);
        await openJob(key);
      }
      if (state.view === "jobs") loadJobs();
    } catch (err) { toast(err.message, true); }
  }

  /* ---------------- 采集中心：默认值跟画像走 ---------------- */
  const collectState = { touched: false };

  function applyCollectDefaults(force = false) {
    const d = state.bootstrap?.collect_defaults;
    if (!d) return;
    if (!force && collectState.touched) return;
    const roles = splitList(state.bootstrap?.profile?.target_roles);
    // 多关键词：画像里有几个目标岗位就全填上（逗号分隔）
    if (d.keyword && roles.length > 1) $("#cKeyword").value = roles.join("，");
    else if (d.keyword) $("#cKeyword").value = d.keyword;
    else $("#cKeyword").value = "";
    $("#cCity").value = d.city || "北京";
    if (d.site) $("#cSite").value = d.site;
    if (d.pages) $("#cPages").value = d.pages;
    collectState.touched = false;
    const src = d.source || {};
    $("#collectHint").textContent =
      `关键词取自${src.keyword || "画像"}，城市取自${src.city || "画像"}` +
      (d.keyword ? "" : "（画像里还没填目标岗位，请手动填关键词）");
    renderKeywordChips();
  }

  function splitList(text) {
    return String(text || "").split(/[,，;；、/|]+/).map((s) => s.trim()).filter(Boolean);
  }

  function currentKeywords() {
    return splitList($("#cKeyword").value);
  }

  function renderKeywordChips() {
    const box = $("#cKeywordChips");
    if (!box) return;
    const roles = splitList(state.bootstrap?.profile?.target_roles);
    const picked = currentKeywords();
    if (!roles.length && !picked.length) { box.innerHTML = ""; return; }
    const chips = [];
    if (roles.length) {
      chips.push(`<span class="muted" style="font-size:12px;margin-right:4px">画像目标岗位：</span>`);
      chips.push(roles.map((r) => {
        const on = picked.includes(r);
        return `<button type="button" class="chip ${on ? "on" : ""}" data-kw="${esc(r)}">${esc(r)}</button>`;
      }).join(""));
    }
    const extra = picked.filter((k) => !roles.includes(k));
    if (extra.length) {
      chips.push(`<span class="muted" style="font-size:12px;margin:0 4px 0 10px">手输：</span>`);
      chips.push(extra.map((k) => `<button type="button" class="chip on" data-kw="${esc(k)}">${esc(k)}</button>`).join(""));
    }
    box.innerHTML = chips.join("");
    $$("#cKeywordChips [data-kw]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const kw = btn.dataset.kw;
        const list = currentKeywords();
        const idx = list.indexOf(kw);
        if (idx >= 0) list.splice(idx, 1);
        else list.push(kw);
        $("#cKeyword").value = list.join("，");
        collectState.touched = true;
        renderKeywordChips();
      });
    });
  }

  async function startPlan() {
    if (scrapeBusy) { toast("已有任务在跑"); return; }
    const keywords = currentKeywords();
    if (!keywords.length) { toast("请填至少一个关键词", true); return; }
    const body = {
      keywords,
      city: $("#cCity").value.trim() || "南京",
      pages: Number($("#cPages").value) || 3,
      auto_detail: !!$("#cAutoDetail")?.checked,
    };
    if (!confirm(`一键采集：4 个平台 × ${keywords.length} 个关键词 = ${4 * keywords.length} 个任务\n\n` +
                 `关键词：${keywords.join("、")}\n城市：${body.city} · 每站 ${body.pages} 页\n\n` +
                 "开始后可以点「终止」随时停。确定开始？")) return;
    try {
      const r = await api("/api/plan", { method: "POST", body });
      toast(r.message || "已开始批量采集");
      scrapeBusy = true;
      $("#scrapeLog").textContent = "（批量任务已启动…）";
      $("#btnScrapeCancel").disabled = false;
      pollScrape();
      loadRuns();
    } catch (err) { toast(err.message, true); }
  }

  /* ---------------- 登录态 ---------------- */
  async function loadLoginState(force = false) {
    let data;
    try { data = await api("/api/login/state"); }
    catch (err) { return; }
    const sig = JSON.stringify(data.sites);
    state.loginSites = data.sites || [];
    // 状态没变化就不重绘，避免每 6 秒打断用户点击
    if (!force && sig === state.loginSig) return;
    state.loginSig = sig;
    const PILL = {
      logged_in: `<span class="pill high">已登录</span>`,
      profile: `<span class="pill medium">登录态未确认</span>`,
      none: `<span class="pill low">未登录</span>`,
    };
    $("#loginSites").innerHTML = state.loginSites.map((s) => {
      let detail = "还没有登录态";
      if (s.status === "logged_in") {
        detail = s.has_state
          ? `登录态文件保存于 ${s.saved_at}（${s.age_hours} 小时前）`
          : `检测到登录 cookie：${s.auth_cookies.join("、")}（${s.saved_at}）`;
      } else if (s.status === "profile") {
        if (s.cookie_error) {
          detail = `读取浏览器 cookie 失败（${s.cookie_error}）——关掉浏览器窗口后重试`;
        } else if (s.cookie_count > 0) {
          detail = `读到 ${s.cookie_count} 条 cookie，但没有登录 token —— 请重新登录`;
        } else {
          detail = "浏览器配置在，但没有登录 cookie —— 可能抓到 0 条或没有薪资";
        }
      }
      return `<div class="doc">
        <div class="info">
          <div class="fname">${esc(s.label)} <span class="muted">· ${esc(s.site)}</span></div>
          <div class="meta">${PILL[s.status] || PILL.none} <span class="muted">${esc(detail)}</span></div>
        </div>
        <div class="ops"><button class="btn sm ${s.status === "logged_in" ? "" : "primary"}" data-login="${esc(s.site)}">${s.status === "logged_in" ? "重新登录" : "登录"}</button></div>
      </div>`;
    }).join("");
    $$("[data-login]").forEach((btn) =>
      btn.addEventListener("click", () => startLogin(btn.dataset.login)));
  }

  function startLoginAutoRefresh() {
    stopLoginAutoRefresh();
    // 登录窗口是外部进程，页面不会自己知道它结束了；采集中心停留期间定时刷新
    state.loginRefreshTimer = setInterval(() => loadLoginState(), 6000);
  }

  function stopLoginAutoRefresh() {
    if (state.loginRefreshTimer) {
      clearInterval(state.loginRefreshTimer);
      state.loginRefreshTimer = null;
    }
  }

  async function startLogin(site) {
    const wait = Number($("#loginWait").value) || 120;
    try {
      await api("/api/login", { method: "POST", body: { site, wait } });
      $("#loginLog").style.display = "";
      $("#loginLog").textContent = "正在打开浏览器…";
      $("#loginStatus").textContent = `正在登录 ${site}，请在弹出的 Chrome 窗口里扫码`;
      $("#btnLoginCancel").disabled = false;
      toast("浏览器已打开，请扫码登录");
      pollLogin();
    } catch (err) { toast(err.message, true); }
  }

  async function pollLogin() {
    clearTimeout(state.loginTimer);
    let st;
    try { st = await api("/api/login/status"); } catch { return; }
    const logBox = $("#loginLog");
    logBox.textContent = (st.log || []).join("\n") || "…";
    logBox.scrollTop = logBox.scrollHeight;

    if (st.running) {
      $("#btnLoginCancel").disabled = false;
      $("#loginStatus").textContent =
        `登录中… 检测到登录会自动保存并关闭窗口（最长 ${st.wait ?? "?"} 秒）`;
      state.loginTimer = setTimeout(pollLogin, 2000);
      return;
    }
    $("#btnLoginCancel").disabled = true;
    if (st.started) {
      const ok = st.exit === 0;
      if (st.cancelled) {
        $("#loginStatus").textContent = "已取消登录（未保存登录态）";
        toast("已取消登录");
      } else if (ok && st.detected) {
        $("#loginStatus").textContent = "检测到登录成功，已保存登录态并关闭窗口";
        toast("登录成功，已自动保存");
      } else if (st.detected === false) {
        $("#loginStatus").textContent = "超时未检测到登录 cookie（当前状态已保存，可再试一次）";
        toast("未检测到登录，请重试", true);
      } else {
        $("#loginStatus").textContent = `登录流程结束（退出码 ${st.exit}）`;
        toast("登录未完成", true);
      }
      loadLoginState(true);
    }
  }

  async function cancelLogin() {
    try {
      await api("/api/login/cancel", { method: "POST" });
      toast("已取消登录");
    } catch (err) { toast(err.message, true); }
    pollLogin();
  }

  /* ---------------- 采集中心 ---------------- */
  let scrapeBusy = false;

  async function startScrape() {
    if (scrapeBusy) { toast("采集进行中…"); return; }
    const body = {
      site: $("#cSite").value,
      keywords: currentKeywords(),
      city: $("#cCity").value.trim() || "北京",
      pages: Number($("#cPages").value) || 1,
      auto_detail: !!$("#cAutoDetail")?.checked,
    };
    if (!body.keywords.length) { toast("请填关键词", true); return; }

    // 没登录态就先问要不要去登录，别让人白抓一轮 0 条
    const info = (state.loginSites || []).find((s) => s.site === body.site);
    if (info && info.status !== "logged_in") {
      const goLogin = confirm(
        `「${info.label}」当前${info.status === "profile" ? "登录态未确认（没有登录 cookie）" : "还没有登录态"}，` +
        "采集很可能只拿到 0 条、或拿不到薪资。\n\n" +
        "点「确定」先去登录（会打开浏览器扫码）\n点「取消」仍然尝试采集",
      );
      if (goLogin) { startLogin(body.site); return; }
    }
    try {
      await api("/api/scrape", { method: "POST", body });
      toast("已启动采集，正在抓取…");
      scrapeBusy = true;
      $("#scrapeLog").textContent = "（任务已启动，等待引擎输出…）";
      $("#btnScrapeCancel").disabled = false;
      pollScrape();
      loadRuns();
    } catch (err) {
      toast(err.message, true);
      $("#scrapeState").textContent = `启动失败：${err.message}`;
    }
  }

  async function cancelScrape() {
    try {
      await api("/api/scrape/cancel", { method: "POST" });
      toast("已终止采集");
    } catch (err) { toast(err.message, true); }
    pollScrape();
  }

  async function pollScrape() {
    clearTimeout(state.pollTimer);
    let st;
    try { st = await api("/api/scrape/status"); } catch { return; }
    const logEl = $("#scrapeLog");
    const stateEl = $("#scrapeState");
    $("#btnScrapeCancel").disabled = !st.running;

    if (st.running) {
      scrapeBusy = true;
      const stage = st.stage === "detail" ? "补全详情中… " : "采集中… ";
      const plan = st.plan_total
        ? `[${st.plan_index}/${st.plan_total}] ${st.plan_current || ""} · `
        : "";
      stateEl.textContent = `${stage}${plan}${st.elapsed != null ? st.elapsed + "s" : ""}`;
      logEl.textContent = (st.log || []).join("\n") || "…";
      logEl.scrollTop = logEl.scrollHeight;
      state.pollTimer = setTimeout(pollScrape, 1500);
      return;
    }

    if (scrapeBusy) {
      scrapeBusy = false;
      const result = st.result || {};
      if (st.cancelled) {
        stateEl.textContent = "已终止本次采集";
      } else if (st.plan_total) {
        const rows2 = st.plan_results || [];
        const created = rows2.reduce((a, x) => a + (x.created || 0), 0);
        stateEl.textContent = `批量完成：${rows2.length}/${st.plan_total} 个任务，共新增 ${created} 条`;
        toast(`批量采集完成，新增 ${created} 条`);
      } else if (st.exit === 0) {
        const skip = result.invalid ? `，字段不全跳过 ${result.invalid}` : "";
        stateEl.textContent =
          `完成：本次抓到 ${result.upstream ?? 0} 条 → 新增 ${result.created ?? 0}，` +
          `已在池中 ${result.updated ?? 0}${skip}（${st.elapsed ?? 0}s）`;
        toast(`采集完成：新增 ${result.created ?? 0} 条`);
      } else {
        stateEl.textContent = `任务结束（退出码 ${st.exit}），未导入新岗位`;
        toast("采集未正常结束", true);
      }
      logEl.textContent = (st.log || []).join("\n") || "（无输出）";
      logEl.scrollTop = logEl.scrollHeight;
      loadRuns();
      loadBootstrap();
      if (state.view === "jobs") loadJobs();
    }
  }

  /* ---------------- 详情补全 / 跨站合并 ---------------- */
  async function startDetailFetch() {
    // 按当前「站点」筛选决定补哪个站；筛的是全部就让用户选
    const filterSite = $("#fSite").value;
    const site = filterSite || (prompt("补哪个站点？输入 boss 或 51job", "boss") || "boss").trim();
    if (!site) return;
    const raw = prompt(`补全多少条 ${site} 岗位的详情？（1-50，会逐条打开岗位页）`, "10");
    if (raw === null) return;
    const limit = Math.min(50, Math.max(1, Number(raw) || 10));
    // 51job 详情页 headless 就能过 WAF，不用弹窗；BOSS 有时需要人工过验证
    const headed = site === "boss"
      ? confirm("BOSS 可能弹人机验证。\n\n【确定】打开浏览器窗口（可人工过验证）\n【取消】后台无窗口运行")
      : false;
    try {
      await api("/api/jobs/fetch-details", { method: "POST", body: { limit, headed, site } });
      toast(`开始补全 ${site} 的 ${limit} 条岗位详情…`);
      pollDetail();
    } catch (err) { toast(err.message, true); }
  }

  async function pollDetail() {
    clearTimeout(state.detailTimer);
    let st;
    try { st = await api("/api/jobs/fetch-details/status"); } catch { return; }
    const btn = $("#btnFetchDetail");
    if (st.running) {
      btn.disabled = true;
      btn.textContent = `补全中… ${st.elapsed ?? 0}s`;
      state.detailTimer = setTimeout(pollDetail, 2000);
      return;
    }
    btn.disabled = false;
    btn.textContent = "补全详情";
    if (st.started) {
      const s = st.summary || {};
      const tail = (st.log || []).slice(-1)[0] || "";
      toast(`详情补全结束：成功 ${s.ok ?? 0}，失败 ${s.fail ?? 0}${st.exit === 0 ? "" : `（${tail}）`}`,
            st.exit !== 0);
      loadJobs();
    }
  }

  async function mergeJobs() {
    try {
      const r = await api("/api/jobs/merge", { method: "POST" });
      toast(r.merged
        ? `合并了 ${r.merged} 组重复岗位，移除 ${r.removed} 条，来源已全部标记`
        : "没有发现跨站点重复的岗位");
      loadJobs();
      loadBootstrap();
    } catch (err) { toast(err.message, true); }
  }

  async function importNow() {
    try {
      const r = await api("/api/import", { method: "POST", body: { refresh: $("#cRefresh").checked } });
      toast(`同步完成：库里 ${r.upstream} 条 → 新增 ${r.created ?? r.imported}，已在池中 ${r.updated ?? 0}`);
      await loadBootstrap();
      loadRuns();
      if (state.view === "jobs") loadJobs();
    } catch (err) { toast(err.message, true); }
  }

  async function loadRuns() {
    let data;
    try { data = await api("/api/scrape/runs"); } catch { return; }
    $("#runsList").innerHTML = data.rows.length ? data.rows.map((r) => `
      <div class="run" data-run="${r.id}">
        <div class="top">
          <b>${esc(r.site || "?")} · ${esc(r.keyword || "")} · ${esc(r.city || "")}</b>
          <span class="pill ${r.status === "done" ? "high" : r.status === "failed" ? "low" : "medium"}">${esc(r.status)}</span>
        </div>
        <div class="meta">${esc(r.started_at || "")}${r.ended_at ? " → " + esc(r.ended_at) : ""}
          · ${r.pages || 0} 页 · 新增 ${r.imported ?? 0} 条
          <a href="#" data-runjobs="${r.id}" style="color:var(--accent2);margin-left:6px">查看岗位</a></div>
        <div class="meta" data-runbox="${r.id}" style="margin-top:6px;display:none"></div>
      </div>`).join("") : `<div class="empty">还没有采集记录</div>`;

    $$("[data-runjobs]").forEach((link) => link.addEventListener("click", async (e) => {
      e.preventDefault();
      const id = link.dataset.runjobs;
      const box = document.querySelector(`[data-runbox="${id}"]`);
      if (!box) return;
      if (box.style.display !== "none") { box.style.display = "none"; return; }
      box.style.display = "";
      box.textContent = "加载中…";
      try {
        const r = await api(`/api/scrape/runs/${id}/jobs`);
        box.innerHTML = r.rows.length
          ? r.rows.map((j) => `<div>· ${esc(j.title)} <span class="muted">· ${esc(j.company || "—")} · ${esc(j.city || "—")}${j.score != null ? ` · ${j.score} 分` : ""}</span></div>`).join("")
          : `<div class="muted">这次采集没有新增岗位</div>`;
      } catch (err) { box.textContent = err.message; }
    }));
  }

  /* ---------------- 申请追踪 ---------------- */
  async function loadApplications() {
    let data;
    try { data = await api("/api/applications"); }
    catch (err) { toast(err.message, true); return; }

    const label = { drafted: "草稿", applied: "已投递", interview: "面试中", offer: "已发 offer", hired: "已入职", rejected: "被拒", no_response: "无回复", offer_declined: "拒 offer", withdrawn: "已撤回" };
    $("#appStats").innerHTML = data.statuses
      .filter((s) => (data.stats[s] || 0) > 0)
      .map((s) => `<span class="chip">${label[s] || s} <b>${data.stats[s]}</b></span>`).join("")
      || `<span class="muted">还没有申请记录</span>`;

    $("#appsBody").innerHTML = data.rows.length ? data.rows.map((a) => `
      <tr data-id="${a.id}">
        <td>${esc(dash(a.date))}</td>
        <td>${esc(a.company)}</td>
        <td class="c-title">${esc(a.role)}</td>
        <td>${esc(dash(a.channel))}</td>
        <td>
          <select class="app-status" data-id="${a.id}">
            ${data.statuses.map((s) => `<option value="${s}" ${s === a.status ? "selected" : ""}>${label[s] || s}</option>`).join("")}
          </select>
        </td>
        <td>${esc(dash(a.fit_rating))}</td>
        <td class="c-title" title="${esc(a.notes || "")}">${esc(a.notes || "")}</td>
        <td><button class="btn sm danger" data-del="${a.id}">删除</button></td>
      </tr>`).join("")
      : `<tr><td colspan="8"><div class="empty">还没有申请记录 —— 可以从岗位池点「标记已投递」生成</div></td></tr>`;

    $$(".app-status").forEach((sel) => sel.addEventListener("change", async () => {
      try {
        await api(`/api/applications/${sel.dataset.id}`, { method: "POST", body: { status: sel.value } });
        toast("状态已更新");
        loadApplications();
      } catch (err) { toast(err.message, true); }
    }));
    $$("[data-del]").forEach((btn) => btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("确定删除这条申请记录？")) return;
      try {
        await api(`/api/applications/${btn.dataset.del}/delete`, { method: "POST" });
        toast("已删除");
        loadApplications();
      } catch (err) { toast(err.message, true); }
    }));
  }

  function openApplicationForm() {
    const today = new Date().toISOString().slice(0, 10);
    $("#dTitle").textContent = "新增申请记录";
    $("#dCompany").textContent = "手动登记一条投递";
    $("#dBody").innerHTML = `
      <div class="form-grid">
        <label>公司<input id="aCompany" placeholder="公司名" /></label>
        <label>职位<input id="aRole" placeholder="岗位名称" /></label>
        <label>投递日期<input id="aDate" type="date" value="${today}" /></label>
        <label>渠道<input id="aChannel" placeholder="BOSS / 官网 / 内推" /></label>
        <label>状态<select id="aStatus">
          <option value="drafted">草稿</option><option value="applied">已投递</option>
          <option value="interview">面试中</option><option value="offer">已发 offer</option>
        </select></label>
        <label>匹配反馈<input id="aFit" placeholder="strong / good" /></label>
        <label class="span2">备注<textarea id="aNotes" rows="3" placeholder="备注（不要用英文逗号）"
          style="background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:9px;padding:8px 11px;font-family:inherit;font-size:13px"></textarea></label>
      </div>`;
    $("#dFoot").innerHTML = `
      <button class="btn primary" id="aSave">保存</button>
      <button class="btn ghost" id="aCancel">取消</button>`;
    $("#aCancel").onclick = closeDrawer;
    $("#aSave").onclick = async () => {
      const body = {
        company: $("#aCompany").value.trim(),
        role: $("#aRole").value.trim(),
        date: $("#aDate").value,
        channel: $("#aChannel").value.trim(),
        status: $("#aStatus").value,
        fit_rating: $("#aFit").value.trim(),
        notes: $("#aNotes").value.trim(),
        source: "webapp",
      };
      if (!body.company || !body.role) { toast("公司和职位必填", true); return; }
      try {
        await api("/api/applications", { method: "POST", body });
        toast("已保存");
        closeDrawer();
        loadApplications();
      } catch (err) { toast(err.message, true); }
    };
    $("#drawer").classList.add("open");
    $("#drawerMask").classList.add("open");
  }

  /* ---------------- 画像 ---------------- */
  const PROFILE_INPUTS = [
    ["name", "#pName"], ["location", "#pLocation"], ["years_experience", "#pYears"],
    ["education_level", "#pEdu"], ["min_salary", "#pSalary"], ["preferred_cities", "#pCities"],
    ["target_roles", "#pRoles"], ["skills_primary", "#pPrimary"],
    ["skills_secondary", "#pSecondary"], ["skills_domain", "#pDomain"],
    ["tools", "#pTools"], ["deal_breakers", "#pBreakers"],
  ];

  const FIELD_LABELS = {
    name: "姓名", location: "城市", years_experience: "工作年限", education_level: "学历",
    skills_primary: "核心技能", skills_secondary: "次要技能", skills_domain: "行业领域",
    tools: "工具", target_roles: "目标岗位", preferred_cities: "期望城市",
    min_salary: "期望月薪(K)", languages: "语言",
  };

  function fillProfile(p, onlyPresent = false) {
    p = p || {};
    for (const [key, sel] of PROFILE_INPUTS) {
      const el = $(sel);
      if (!el) continue;
      const val = p[key];
      if (onlyPresent && (val === undefined || val === null || val === "")) continue;
      el.value = val ?? "";
    }
  }

  /* ---------------- 简历上传与档案生成 ---------------- */
  function fmtSize(bytes) {
    if (!bytes && bytes !== 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  }

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || "");
        const idx = result.indexOf(",");
        resolve(idx >= 0 ? result.slice(idx + 1) : result);
      };
      reader.onerror = () => reject(new Error(`读取 ${file.name} 失败`));
      reader.readAsDataURL(file);
    });
  }

  async function loadDocuments() {
    let data;
    try { data = await api("/api/documents"); }
    catch (err) { toast(err.message, true); return; }

    const hint = $("#engineHint");
    if (hint) {
      hint.textContent = data.llm?.configured
        ? `LLM 分析（${data.llm.model}）`
        : "本地规则解析（未配置 LLM，可选）";
    }
    if ($("#llmBase")) $("#llmBase").value = data.llm?.base_url || "";
    if ($("#llmModel")) $("#llmModel").value = data.llm?.model || "deepseek-chat";
    if ($("#llmNoThink")) $("#llmNoThink").checked = data.llm?.disable_thinking !== false;
    if ($("#llmStatus")) {
      $("#llmStatus").textContent = data.llm?.configured
        ? `已配置：${data.llm.base_url} · 模型 ${data.llm.model}。分析时会优先用 LLM，失败自动回落规则解析。`
        : "未配置时使用本地规则解析，离线可用。填写 OpenAI 兼容的 base_url + key 即可启用 LLM。";
    }

    const badge = { cv: "简历", linkedin: "LinkedIn", diplomas: "学历", references: "推荐信", projects: "项目", applications: "申请", other: "其他" };
    $("#docList").innerHTML = data.rows.length ? data.rows.map((d) => {
      const statusPill = d.status === "parsed"
        ? `<span class="pill high">已解析</span>`
        : `<span class="pill low" title="${esc(d.message || "")}">待处理</span>`;
      const analyzed = d.extracted?.profile && Object.keys(d.extracted.profile).length
        ? `<span class="pill medium">已生成档案</span>` : "";
      return `<div class="doc">
        <div class="info">
          <div class="fname">${esc(d.filename)}</div>
          <div class="meta">${badge[d.category] || d.category} · ${fmtSize(d.size_bytes)} · ${d.text_chars || 0} 字
            · ${esc((d.created_at || "").slice(0, 16))} · ${esc(d.rel_path || "")}</div>
        </div>
        <div class="ops">${statusPill}${analyzed}
          <button class="btn sm" data-analyze="${d.id}">分析生成档案</button>
          <button class="btn sm danger" data-deldoc="${d.id}">删除</button>
        </div>
      </div>`;
    }).join("") : `<div class="muted" style="padding:4px 2px">还没有上传文件</div>`;

    $$("[data-analyze]").forEach((btn) =>
      btn.addEventListener("click", () => analyzeDocument(btn.dataset.analyze)));
    $$("[data-deldoc]").forEach((btn) =>
      btn.addEventListener("click", async () => {
        if (!confirm("删除这份文件记录？（不会删除 documents/ 里的原文件）")) return;
        try {
          await api(`/api/documents/${btn.dataset.deldoc}/delete`, { method: "POST" });
          toast("已删除");
          loadDocuments();
        } catch (err) { toast(err.message, true); }
      }));
  }

  async function uploadFiles(files) {
    if (!files || !files.length) return;
    const category = $("#docCategory").value;
    for (const file of Array.from(files)) {
      $("#dropzone").classList.add("uploading");
      try {
        const content_base64 = await fileToBase64(file);
        const r = await api("/api/documents", {
          method: "POST",
          body: { filename: file.name, category, content_base64 },
        });
        if (r.ok) toast(`${file.name} 已解析 ${r.chars} 字，可点「分析生成档案」`);
        else toast(`${file.name}：${r.error || "没有抽取到文本"}`, true);
      } catch (err) {
        toast(`${file.name} 上传失败：${err.message}`, true);
      } finally {
        $("#dropzone").classList.remove("uploading");
      }
    }
    $("#fileInput").value = "";
    loadDocuments();
  }

  async function analyzeDocument(id) {
    try {
      toast("正在分析…");
      const r = await api(`/api/documents/${id}/analyze`, { method: "POST", body: {} });
      renderDocResult(r);
      loadDocuments();
    } catch (err) { toast(err.message, true); }
  }

  function renderDocResult(result) {
    const profile = result.profile || {};
    const evidence = result.evidence || {};
    const DISPLAY_ORDER = ["name", "location", "preferred_cities", "years_experience",
      "education_level", "target_roles", "skills_primary", "skills_secondary",
      "skills_domain", "tools", "min_salary", "languages"];
    const keys = Object.keys(profile).sort((a, b) => {
      const ia = DISPLAY_ORDER.indexOf(a), ib = DISPLAY_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    if (!keys.length) {
      $("#docResult").innerHTML = `<div class="docresult"><h3>没有识别到可用字段</h3>
        <div class="notes">这份文件的文本里没有匹配到姓名/技能/学历等特征，请手动填写画像。</div></div>`;
      return;
    }
    const rows = keys.map((key) => {
      const val = Array.isArray(profile[key])
        ? profile[key].map((x) => (typeof x === "object" ? `${x.language || ""}${x.level ? "(" + x.level + ")" : ""}` : x)).join("、")
        : profile[key];
      const ev = evidence[key] ? `<div class="ev">依据：${esc(String(evidence[key]).slice(0, 120))}</div>` : "";
      return `<div class="k">${FIELD_LABELS[key] || esc(key)}</div><div>${esc(String(val).slice(0, 220))}${ev}</div>`;
    }).join("");

    const notes = (result.notes || []).map((n) => `<div>· ${esc(n)}</div>`).join("");
    $("#docResult").innerHTML = `<div class="docresult">
      <h3>分析结果 <span class="muted">· ${esc(result.engine === "llm" ? "LLM" : "本地规则")} · ${esc(result.chars || 0)} 字</span></h3>
      <div class="fieldgrid">${rows}</div>
      ${notes ? `<div class="notes">${notes}</div>` : ""}
      <div class="actions">
        <button class="btn primary" id="btnApplyDraft">填入画像表单</button>
        <button class="btn" id="btnApplySave">填入并保存</button>
      </div>
    </div>`;
    window.__lastDraft = profile;
    $("#btnApplyDraft").onclick = () => {
      fillProfile(profile, true);
      toast("已填入表单，请核对后保存");
    };
    $("#btnApplySave").onclick = async () => {
      fillProfile(profile, true);
      await saveProfile();
      $("#docResult").innerHTML = "";
    };
  }

  /* 模型选择：点输入框就自动拉取并弹出列表 */
  const modelPicker = { cache: [], cacheKey: "", active: 0 };

  // 拿不到列表时的兜底建议（只给常见写法，仍需「测试连接」验证）
  const MODEL_SUGGESTIONS = [
    { match: "deepseek", models: ["deepseek-chat", "deepseek-reasoner"] },
    { match: "openai", models: ["gpt-4o-mini", "gpt-4o"] },
    { match: "moonshot", models: ["kimi-k2-0905-preview", "moonshot-v1-8k"] },
    { match: "dashscope", models: ["qwen-plus", "qwen-max", "qwen-turbo"] },
    { match: "bigmodel", models: ["glm-4-plus", "glm-4-flash"] },
    { match: "siliconflow", models: ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"] },
  ];

  function friendlyLlmError(message) {
    const text = String(message || "");
    let head = "";
    if (text.includes("HTTP 404") || text.includes("/models 接口")) {
      head = "该服务没有 /models 接口";
    } else if (text.includes("HTTP 401")) {
      head = "API Key 无效或已过期（401）";
    } else if (text.includes("HTTP 403")) {
      head = "没有访问权限（403）";
    } else if (text.includes("HTTP 429")) {
      head = "请求过于频繁或额度用尽（429）";
    } else if (text.includes("HTTP 400")) {
      head = "请求被拒绝（400）";
    }
    const detail = text.length > 160 ? `${text.slice(0, 160)}…` : text;
    return head ? `${head}：${detail}` : detail;
  }

  function closeModelPanel() {
    const panel = $("#llmModelPanel");
    if (panel) panel.hidden = true;
  }

  function bindModelItems(panel) {
    panel.querySelectorAll(".combo-item").forEach((item) => {
      item.addEventListener("mousedown", (e) => {
        e.preventDefault();
        $("#llmModel").value = item.dataset.model;
        closeModelPanel();
        $("#llmProbe").textContent = `已选 ${item.dataset.model}，可点「测试连接」确认`;
      });
    });
  }

  function suggestionBlock(base) {
    const hit = MODEL_SUGGESTIONS.find((s) => base.toLowerCase().includes(s.match));
    if (!hit) {
      return `<div class="combo-tip">也可以直接手填模型名（在服务商控制台能看到具体 id）</div>`;
    }
    return `<div class="combo-tip">这家拉不到列表，常见模型可直接点选（建议随后点「测试连接」确认）：</div>` +
      hit.models.map((m) => `<div class="combo-item" data-model="${esc(m)}">${esc(m)}</div>`).join("");
  }

  /* 模糊查询：子序列也算命中（输入 dsc 能命中 deepseek-chat），按相关度排序 */
  function fuzzyScore(text, query) {
    if (!query) return 0;
    const target = String(text || "").toLowerCase();
    const needle = query.toLowerCase();
    if (!target) return -1;
    if (target === needle) return 1000;
    if (target.startsWith(needle)) return 900 - Math.min(99, target.length - needle.length);
    const at = target.indexOf(needle);
    if (at >= 0) return 800 - Math.min(99, at);
    // 分词后再比前缀：输入 qwen 命中 qwen-plus 这类
    const tokens = target.split(/[-_./:\s]+/).filter(Boolean);
    if (tokens.some((tok) => tok.startsWith(needle))) return 700;
    // 子序列：按顺序出现即可，越紧凑分越高
    let cursor = 0;
    let gaps = 0;
    let last = -1;
    for (const ch of needle) {
      const found = target.indexOf(ch, cursor);
      if (found < 0) return -1;
      if (last >= 0) gaps += found - last - 1;
      last = found;
      cursor = found + 1;
    }
    return Math.max(50, 600 - gaps * 8 - (target.length - needle.length));
  }

  function searchModels(query) {
    const needle = (query || "").trim();
    const scored = modelPicker.cache.map((m) => {
      const idScore = fuzzyScore(m.id, needle);
      const ownerScore = m.owned_by ? fuzzyScore(m.owned_by, needle) : -1;
      return { ...m, score: Math.max(idScore, ownerScore >= 0 ? ownerScore - 250 : -1) };
    });
    if (!needle) return scored;
    return scored
      .filter((m) => m.score >= 0)
      .sort((a, b) => b.score - a.score || a.id.localeCompare(b.id));
  }

  function highlightMatch(text, query) {
    const raw = String(text ?? "");
    const needle = (query || "").trim().toLowerCase();
    if (!needle) return esc(raw);
    let out = "";
    let cursor = 0;
    let open = false;
    for (const ch of raw) {
      const hit = cursor < needle.length && ch.toLowerCase() === needle[cursor];
      if (hit) cursor += 1;
      if (hit && !open) { out += "<mark>"; open = true; }
      if (!hit && open) { out += "</mark>"; open = false; }
      out += esc(ch);
    }
    return open ? `${out}</mark>` : out;
  }

  function renderModelOptions(filter = "") {
    const panel = $("#llmModelPanel");
    const query = (filter || "").trim();
    const list = searchModels(query);
    const typed = $("#llmModel").value.trim();
    const useTyped = typed && !modelPicker.cache.some((m) => m.id === typed);

    if (!list.length && !useTyped) {
      panel.innerHTML = `<div class="combo-tip">没有匹配「${esc(query)}」的模型` +
        `${modelPicker.cache.length ? `（共 ${modelPicker.cache.length} 个）` : ""}</div>`;
      return;
    }
    if (modelPicker.active >= list.length) modelPicker.active = 0;

    const head = `<div class="combo-head"><span>${
      query ? `匹配 ${list.length} / ${modelPicker.cache.length} 个` : `共 ${modelPicker.cache.length} 个模型`
    }</span><button type="button" id="btnReloadModels">重新拉取</button></div>`;

    const typedRow = useTyped
      ? `<div class="combo-item" data-model="${esc(typed)}"><span class="muted">直接使用：</span>${esc(typed)}</div>`
      : "";

    const rows = list.map((m, index) => `<div class="combo-item${index === modelPicker.active ? " active" : ""}" data-model="${esc(m.id)}">${
      highlightMatch(m.id, query)
    }${m.owned_by ? `<span class="owner">${esc(m.owned_by)}</span>` : ""}</div>`).join("");

    // 手输行放最后：有匹配时回车应选中最佳匹配，而不是未经验证的输入
    panel.innerHTML = head + rows + typedRow;
    bindModelItems(panel);
    const reload = panel.querySelector("#btnReloadModels");
    if (reload) {
      reload.addEventListener("mousedown", (e) => { e.preventDefault(); loadModels(true); });
    }
  }

  async function loadModels(force = false) {
    const panel = $("#llmModelPanel");
    const base = $("#llmBase").value.trim();
    const key = $("#llmKey").value.trim();
    panel.hidden = false;

    if (!base) {
      panel.innerHTML = `<div class="combo-tip">先填 Base URL，再点这里</div>`;
      return;
    }
    const cacheKey = `${base}|${key}`;
    if (!force && modelPicker.cache.length && modelPicker.cacheKey === cacheKey) {
      renderModelOptions($("#llmModel").value.trim());
      return;
    }
    panel.innerHTML = `<div class="combo-tip">正在拉取模型列表…</div>`;
    try {
      const r = await api("/api/llm/models", {
        method: "POST",
        body: { base_url: base, api_key: key },
      });
      modelPicker.cache = r.models || [];
      modelPicker.cacheKey = cacheKey;
      if (!modelPicker.cache.length) {
        panel.innerHTML = `<div class="combo-tip">该服务没有返回模型列表，请直接手填模型名</div>` +
          suggestionBlock(base);
        bindModelItems(panel);
        return;
      }
      $("#llmProbe").textContent = `已获取 ${r.count} 个模型 · ${r.latency_ms}ms`;
      renderModelOptions($("#llmModel").value.trim());
    } catch (err) {
      panel.innerHTML = `<div class="combo-tip err">拉取失败：${esc(friendlyLlmError(err.message))}</div>` +
        suggestionBlock(base);
      bindModelItems(panel);
    }
  }

  async function testLlm() {
    const base = $("#llmBase").value.trim();
    const model = $("#llmModel").value.trim();
    if (!base) { toast("先填 Base URL", true); return; }
    if (!model) { toast("先选一个模型", true); return; }
    const probe = $("#llmProbe");
    probe.textContent = "正在测试连接…";
    try {
      const r = await api("/api/llm/test", {
        method: "POST",
        body: { base_url: base, api_key: $("#llmKey").value.trim(), model },
      });
      const reply = r.empty ? "接口连通，但模型这次返回了空内容" : `回复「${r.reply}」`;
      probe.textContent = `✓ 连接正常 · ${r.model} · ${r.latency_ms}ms · ${reply}`;
      toast("连接测试通过");
    } catch (err) {
      probe.textContent = `✕ 测试失败：${friendlyLlmError(err.message)}`;
      toast(friendlyLlmError(err.message), true);
    }
  }

  async function saveLlmSettings() {
    const body = {
      llm_base_url: $("#llmBase").value.trim(),
      llm_model: $("#llmModel").value.trim(),
      llm_api_key: $("#llmKey").value.trim(),
      llm_disable_thinking: $("#llmNoThink").checked,
    };
    if (!body.llm_base_url) { toast("请填 Base URL", true); return; }
    try {
      const r = await api("/api/settings", { method: "POST", body });
      $("#llmKey").value = "";
      if (state.bootstrap) state.bootstrap.llm = r.llm;
      $("#llmStatus").textContent = r.llm.configured
        ? `已配置：${r.llm.base_url} · 模型 ${r.llm.model}`
        : "已保存地址，但还缺 API Key，仍用本地规则解析。";
      await loadDocuments();
      toast("引擎设置已保存");
      // 让下次点开模型时按新配置重新拉取
      modelPicker.cacheKey = "";
    } catch (err) { toast(err.message, true); }
  }

  /* ---------------- 简历工坊 ---------------- */
  let lastStudio = null;

  async function loadResumeSources() {
    let data;
    try { data = await api("/api/resume/sources"); }
    catch (err) { toast(err.message, true); return; }

    state.studioJobs = data.jobs || [];
    $("#rJob").innerHTML = `<option value="">— 手动填写 —</option>` + state.studioJobs.map((j) =>
      `<option value="${esc(j.job_key)}">${esc(j.title)} · ${esc(j.company || "—")}${j.score != null ? ` (${j.score})` : ""}</option>`
    ).join("");

    $("#rDoc").innerHTML = `<option value="">— 手动填写 —</option>` + (data.documents || []).map((d) =>
      `<option value="${d.id}">${esc(d.filename)}（${d.chars} 字）</option>`).join("");

    const p = data.plugin || {};
    $("#rPlugin").innerHTML = p.ok
      ? `<span class="pill high">引擎就绪</span> <span class="muted">guard ${esc(p.guard || "?")}</span>`
      : `<span class="pill low">引擎不可用</span> <span class="muted">${esc(p.error || p.plugin_dir || "")}</span>`;
    if (data.llm) $("#rEngine").value = "auto";
    loadVersions();
  }

  /* ---------------- 改写历史 ---------------- */
  function verificationText(v) {
    if (!v) return "";
    const lines = [`校验：${v.ok ? "通过" : "未通过"}${v.guard_version ? `（guard ${v.guard_version}）` : ""}`];
    (v.findings || []).forEach((f) => lines.push(`[${f.severity || "info"}] ${f.message}`));
    return lines.join("\n");
  }

  async function loadVersions() {
    const box = $("#rHistory");
    if (!box) return;
    let data;
    try { data = await api("/api/resume/versions"); }
    catch { return; }
    if (!data.rows.length) {
      box.innerHTML = `<div class="muted" style="padding:4px 2px">还没有生成过改写结果</div>`;
      return;
    }
    box.innerHTML = data.rows.map((v) => `
      <div class="run" data-version="${v.id}" style="cursor:pointer">
        <div class="top">
          <b>${esc(v.job_title || "（未关联岗位）")}${v.company ? ` · ${esc(v.company)}` : ""}</b>
          <span class="pill ${v.verification_ok ? "high" : "low"}">${v.verification_ok ? "校验通过" : "有告警"}</span>
        </div>
        <div class="meta">
          #${v.id} · ${esc((v.created_at || "").slice(0, 16))} · ${esc(v.engine === "llm" ? "LLM" : "本地重排")}
          · ${v.chars || 0} 字 · 来源 ${esc(v.resume_source || "—")}
        </div>
        <div class="meta" style="margin-top:4px">
          <button class="btn sm" data-load="${v.id}">载入</button>
          <button class="btn sm danger" data-delver="${v.id}">删除</button>
        </div>
      </div>`).join("");

    box.querySelectorAll("[data-load]").forEach((btn) =>
      btn.addEventListener("click", (e) => { e.stopPropagation(); loadVersion(btn.dataset.load); }));
    box.querySelectorAll("[data-delver]").forEach((btn) =>
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("删除这条历史版本？")) return;
        try {
          await api(`/api/resume/versions/${btn.dataset.delver}/delete`, { method: "POST" });
          toast("已删除");
          loadVersions();
        } catch (err) { toast(err.message, true); }
      }));
  }

  async function loadVersion(id) {
    let v;
    try { v = await api(`/api/resume/versions/${id}`); }
    catch (err) { toast(err.message, true); return; }
    lastStudio = {
      engine: v.engine,
      note: `历史版本 #${v.id} · ${(v.created_at || "").slice(0, 16)}`,
      attempts: 0,
      rewritten: v.resume_text || "",
      changes: v.changes_text || "",
      gaps: v.gaps || [],
      verification: v.verification || {},
      verification_text: verificationText(v.verification || {}),
    };
    renderStudio(lastStudio);
    $("#rResultCard").scrollIntoView({ behavior: "smooth", block: "start" });
    toast(`已载入历史版本 #${v.id}`);
  }

  async function openStudioForJob(job) {
    closeDrawer();
    switchView("studio");
    // 等 sources 加载完再把该岗位选上
    for (let i = 0; i < 25 && !(state.studioJobs || []).length; i++) {
      await new Promise((r) => setTimeout(r, 150));
    }
    const select = $("#rJob");
    const exists = Array.from(select.options).some((o) => o.value === job.job_key);
    if (exists) {
      select.value = job.job_key;
      select.dispatchEvent(new Event("change"));
    } else {
      $("#rJd").value = [job.title, job.company, job.skills, job.description]
        .filter(Boolean).join("\n");
    }
    $("#rResume").focus();
    toast("已带入该岗位的 JD，填上简历后点「生成改写」");
  }

  async function runRewrite() {
    const jd = $("#rJd").value.trim();
    const resume = $("#rResume").value.trim();
    if (jd.length < 10) { toast("JD 太短，至少贴一段岗位描述", true); return; }
    if (resume.length < 20) { toast("简历内容太短", true); return; }

    $("#btnRewrite").disabled = true;
    $("#rStatus").textContent = "正在生成…";
    try {
      const r = await api("/api/resume/generate", {
        method: "POST",
        body: {
          jd, resume,
          engine: $("#rEngine").value,
          job_key: $("#rJob").value || null,
        },
      });
      lastStudio = r;
      renderStudio(r);
      loadVersions();
    } catch (err) {
      toast(err.message, true);
      $("#rStatus").textContent = `失败：${err.message}`;
    } finally {
      $("#btnRewrite").disabled = false;
    }
  }

  function renderStudio(r) {
    const v = r.verification || {};
    const stats = v.stats || {};
    const changes = String(r.changes || "").split("\n").map((s) => s.replace(/^-\s*/, "")).filter(Boolean);
    const gaps = r.gaps || [];
    const findings = v.findings || [];

    $("#rResultCard").style.display = "";
    $("#rResult").innerHTML = `
      <div class="muted" style="margin-bottom:10px">
        引擎：<b>${r.engine === "llm" ? "LLM 改写" : "本地重排"}</b>
        ${r.attempts ? ` · 尝试 ${r.attempts} 次` : ""} · ${esc(r.note || "")}
      </div>
      <div class="resblock"><h3>优化后简历</h3><pre>${esc(r.rewritten)}</pre></div>
      <div class="resblock"><h3>改动说明</h3>
        ${changes.length ? `<ul>${changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : `<div class="muted">无</div>`}
      </div>
      <div class="resblock"><h3>待补充清单（JD 要求但简历未出现）</h3>
        ${gaps.length
          ? `<div class="gapchips">${gaps.map((g) => `<span class="gapchip">${esc(g)}</span>`).join("")}</div>`
          : `<div class="muted">没有发现缺口</div>`}
      </div>
      <div class="resblock"><h3>校验结果</h3>
        <div class="verdict ${v.ok ? "pass" : "fail"}">
          ${v.ok ? "✓ 校验通过" : "✕ 校验未通过"}
          <span class="muted">guard ${esc(v.guard_version || "?")}</span>
        </div>
        <div class="muted" style="margin-bottom:8px">
          数字 ${stats.originalNumbers ?? "?"}→${stats.rewrittenNumbers ?? "?"} ·
          实体 ${stats.originalEntities ?? "?"}→${stats.rewrittenEntities ?? "?"}
        </div>
        ${findings.length
          ? findings.map((f) => `<div class="finding">
              <span class="sev ${esc(f.severity || "info")}">${esc(f.severity || "info")}</span>
              <span>${esc(f.message)}${f.detail ? `<br><span class="muted">${esc(f.detail)}</span>` : ""}</span>
            </div>`).join("")
          : `<div class="muted">没有发现问题</div>`}
      </div>`;
    $("#rStatus").textContent = `完成 · ${r.rewritten.trim().length} 字`;
  }

  async function exportStudioDocx() {
    if (!lastStudio) { toast("先生成一次改写", true); return; }
    try {
      const r = await api("/api/resume/export", {
        method: "POST",
        body: {
          resume: lastStudio.rewritten,
          changes: lastStudio.changes,
          gaps: (lastStudio.gaps || []).map((g) => `- ${g}`).join("\n"),
          verification: lastStudio.verification_text || "",
          filename: `简历-${new Date().toISOString().slice(0, 10)}`,
        },
      });
      const binary = atob(r.base64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const blob = new Blob([bytes], {
        type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = r.filename || "resume.docx";
      link.click();
      URL.revokeObjectURL(url);
      toast("已下载 .docx");
    } catch (err) { toast(err.message, true); }
  }

  async function copyStudioResume() {
    if (!lastStudio) { toast("先生成一次改写", true); return; }
    try {
      await navigator.clipboard.writeText(lastStudio.rewritten);
      toast("简历已复制到剪贴板");
    } catch {
      toast("浏览器拒绝了剪贴板访问，请手动选中复制", true);
    }
  }

  async function saveProfile() {
    const body = {
      name: $("#pName").value.trim(),
      location: $("#pLocation").value.trim(),
      years_experience: $("#pYears").value,
      education_level: $("#pEdu").value,
      min_salary: $("#pSalary").value,
      preferred_cities: $("#pCities").value.trim(),
      target_roles: $("#pRoles").value.trim(),
      skills_primary: $("#pPrimary").value.trim(),
      skills_secondary: $("#pSecondary").value.trim(),
      skills_domain: $("#pDomain").value.trim(),
      tools: $("#pTools").value.trim(),
      deal_breakers: $("#pBreakers").value.trim(),
    };
    try {
      const r = await api("/api/profile", { method: "POST", body });
      // 画像已经存进库了；这里只是同步前端缓存，别让空 bootstrap 把成功变成报错
      if (state.bootstrap) state.bootstrap.profile = r.profile;
      $("#profileSaved").textContent = `已保存，重算 ${r.rescored ?? 0} 条岗位分数`;
      toast("画像已保存");
      // 画像变了，采集中心的默认关键词/城市也跟着刷新（用户手改过就不覆盖）
      collectState.touched = false;
      await loadBootstrap();
      setTimeout(() => { $("#profileSaved").textContent = ""; }, 5000);
    } catch (err) {
      $("#profileSaved").textContent = "";
      toast(err.message, true);
    }
  }

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    $$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));
    $("#btnRefresh").addEventListener("click", refreshAll);
    $("#dClose").addEventListener("click", closeDrawer);
    $("#drawerMask").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

    let searchTimer;
    $("#fQ").addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { state.jobs.offset = 0; loadJobs(); }, 260);
    });
    ["#fCity", "#fSite", "#fFit", "#fStatus", "#fSort"].forEach((sel) =>
      $(sel).addEventListener("change", () => { state.jobs.offset = 0; loadJobs(); }));
    $("#btnPrev").addEventListener("click", () => {
      state.jobs.offset = Math.max(0, state.jobs.offset - state.jobs.limit); loadJobs();
    });
    $("#btnNext").addEventListener("click", () => {
      state.jobs.offset += state.jobs.limit; loadJobs();
    });
    $("#btnRescoreAll").addEventListener("click", async () => {
      try {
        const r = await api("/api/rescore", { method: "POST" });
        toast(`已重算 ${r.rescored} 条岗位`);
        loadJobs();
      } catch (err) { toast(err.message, true); }
    });
    $("#btnFetchDetail").addEventListener("click", startDetailFetch);
    $("#btnMergeJobs").addEventListener("click", mergeJobs);
    $("#btnCleanup").addEventListener("click", async () => {
      if (!confirm("删除所有「已跳过」和「已过期」的岗位？申请记录会保留。")) return;
      try {
        const r = await api("/api/jobs/cleanup", {
          method: "POST",
          body: { statuses: ["skipped", "expired"] },
        });
        toast(`已清理 ${r.removed} 条岗位`);
        state.jobs.offset = 0;
        await loadJobs();
        loadBootstrap();
      } catch (err) { toast(err.message, true); }
    });

    $("#checkAll").addEventListener("change", (e) => {
      const on = e.target.checked;
      $$("#jobsBody .row-check").forEach((box) => {
        box.checked = on;
        if (on) state.selected.add(box.dataset.key);
        else state.selected.delete(box.dataset.key);
        box.closest("tr").classList.toggle("selected", on);
      });
      syncSelection();
    });

    $("#btnBatchDelete").addEventListener("click", async () => {
      const keys = [...state.selected];
      if (!keys.length) return;
      if (!confirm(`删除选中的 ${keys.length} 条岗位？申请记录会保留，此操作不可恢复。`)) return;
      try {
        const r = await api("/api/jobs/batch-delete", { method: "POST", body: { keys } });
        state.selected.clear();
        toast(`已删除 ${r.removed} 条岗位`);
        await loadJobs();
        loadBootstrap();
      } catch (err) { toast(err.message, true); }
    });

    $("#btnScrape").addEventListener("click", startScrape);
    $("#btnPlan").addEventListener("click", startPlan);
    $("#btnScrapeCancel").addEventListener("click", cancelScrape);
    $("#btnImport").addEventListener("click", importNow);
    $("#btnLoginCancel").addEventListener("click", cancelLogin);
    $("#btnLoginRefresh").addEventListener("click", async () => {
      await loadLoginState(true);
      toast("登录态已刷新");
    });
    $("#btnCollectReset").addEventListener("click", () => {
      applyCollectDefaults(true);
      toast("已套用画像里的默认关键词与城市");
    });
    // 用户手动改过就不再覆盖
    ["#cKeyword", "#cCity"].forEach((sel) =>
      $(sel).addEventListener("input", () => {
        collectState.touched = true;
        if (sel === "#cKeyword") renderKeywordChips();
      }));
    $("#btnAddApp").addEventListener("click", openApplicationForm);
    $("#btnSaveProfile").addEventListener("click", saveProfile);
    $("#btnSaveLlm").addEventListener("click", saveLlmSettings);
    $("#btnTestLlm").addEventListener("click", testLlm);
    // 勾选框改动即时生效，不必等「保存设置」
    $("#llmNoThink").addEventListener("change", async () => {
      try {
        const r = await api("/api/settings", {
          method: "POST",
          body: { llm_disable_thinking: $("#llmNoThink").checked },
        });
        if (state.bootstrap) state.bootstrap.llm = r.llm;
        $("#llmProbe").textContent = $("#llmNoThink").checked
          ? "已关闭模型思考（更快、更省 token）"
          : "已开启模型思考（更慢、更费 token）";
      } catch (err) { toast(err.message, true); }
    });
    $("#llmPreset").addEventListener("change", () => {
      const [base, model] = $("#llmPreset").value.split("|");
      if (!base) return;
      $("#llmBase").value = base;
      if (model) $("#llmModel").value = model;
      modelPicker.cacheKey = "";           // 配置变了，下次点开重新拉
      modelPicker.cache = [];
      $("#llmProbe").textContent = `已填入 ${base}`;
    });

    // 模型输入框：聚焦/点击即自动拉取并弹出列表
    const modelInput = $("#llmModel");
    modelInput.addEventListener("focus", () => { modelPicker.active = 0; loadModels(); });
    modelInput.addEventListener("click", () => {
      if ($("#llmModelPanel").hidden) { modelPicker.active = 0; loadModels(); }
    });
    modelInput.addEventListener("input", () => {
      if (!modelPicker.cache.length) return;
      modelPicker.active = 0;
      $("#llmModelPanel").hidden = false;
      renderModelOptions(modelInput.value.trim());
    });
    modelInput.addEventListener("keydown", (e) => {
      const panel = $("#llmModelPanel");
      if (e.key === "Escape") { closeModelPanel(); modelInput.blur(); return; }
      if (panel.hidden) return;
      const items = Array.from(panel.querySelectorAll(".combo-item"));
      if (!items.length) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        modelPicker.active = (modelPicker.active + step + items.length) % items.length;
        items.forEach((item, i) => item.classList.toggle("active", i === modelPicker.active));
        items[modelPicker.active].scrollIntoView({ block: "nearest" });
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        const chosen = items[Math.min(modelPicker.active, items.length - 1)];
        if (!chosen) return;
        modelInput.value = chosen.dataset.model;
        closeModelPanel();
        $("#llmProbe").textContent = `已选 ${chosen.dataset.model}，可点「测试连接」确认`;
      }
    });
    document.addEventListener("mousedown", (e) => {
      if (!e.target.closest || !e.target.closest("#llmModelCombo")) closeModelPanel();
    });

    // 简历工坊
    $("#btnRewrite").addEventListener("click", runRewrite);
    $("#btnExportDocx").addEventListener("click", exportStudioDocx);
    $("#btnCopyResume").addEventListener("click", copyStudioResume);
    $("#rJob").addEventListener("change", () => {
      const job = (state.studioJobs || []).find((j) => j.job_key === $("#rJob").value);
      if (job) $("#rJd").value = job.jd;
    });
    $("#rDoc").addEventListener("change", async () => {
      const id = $("#rDoc").value;
      if (!id) return;
      try {
        const doc = await api(`/api/documents/${id}`);
        $("#rResume").value = doc.content || "";
      } catch (err) { toast(err.message, true); }
    });

    // 拖拽上传
    const dz = $("#dropzone");
    $("#btnPick").addEventListener("click", () => $("#fileInput").click());
    $("#fileInput").addEventListener("change", (e) => uploadFiles(e.target.files));
    ["dragenter", "dragover"].forEach((evt) =>
      dz.addEventListener(evt, (e) => { e.preventDefault(); dz.classList.add("over"); }));
    ["dragleave", "drop"].forEach((evt) =>
      dz.addEventListener(evt, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
    dz.addEventListener("drop", (e) => uploadFiles(e.dataTransfer?.files));
    // 防止拖到页面空白处时浏览器直接打开文件
    ["dragover", "drop"].forEach((evt) =>
      window.addEventListener(evt, (e) => { if (e.target.closest?.("#dropzone") === null) e.preventDefault(); }));
  }

  async function init() {
    bind();
    window.addEventListener("hashchange", () => switchView(viewFromHash()));
    try { await loadBootstrap(); }
    catch (err) { toast(`初始化失败：${err.message}`, true); }
    switchView(viewFromHash());
  }

  document.addEventListener("DOMContentLoaded", init);
})();
