"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const TERMINAL = new Set(["success", "failed", "unknown", "rehearsed"]);
  const state = {
    bootstrap: null, mode: "sample", videos: [], selected: null, analysis: null,
    validation: null, validating: false, loadingVideos: false, analyzing: false,
    submitting: false, settingsSaving: false, job: null, jobPollingError: false,
    confirmSnapshot: null, pollTimer: null, validationTimer: null, toastTimer: null,
    videoRequest: 0, analysisRequest: 0, validationRequest: 0, historyRequest: 0,
  };

  const svgNS = "http://www.w3.org/2000/svg";
  function icon(kind) {
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("fill", "none");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(svgNS, "path");
    path.setAttribute("d", kind === "check" ? "m4 10 4 4 8-8" : kind === "cross" ? "m6 6 8 8M14 6l-8 8" : "m7 5 8 5-8 5V5Z");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", kind === "play" ? "1.3" : "1.6");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
    return svg;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function put(id, value) { $(id).textContent = value === undefined || value === null ? "—" : String(value); }
  function hide(id, yes = true) { $(id).hidden = yes; }
  function number(value) { return Number.isFinite(Number(value)) ? Number(value).toLocaleString("ko-KR") : "—"; }
  function dateText(value, withTime = false) {
    if (!value) return "날짜 정보 없음";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return String(value);
    const options = { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" };
    if (withTime) Object.assign(options, { hour: "2-digit", minute: "2-digit", hour12: false });
    return new Intl.DateTimeFormat("ko-KR", options).format(date);
  }

  function timeText(value) {
    if (!value) return "";
    if (/^\d{2}:\d{2}/.test(String(value))) return String(value).slice(0, 8);
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date) : "";
  }

  function safeUrl(value, allowLocal = false) {
    if (typeof value !== "string" || !value.trim()) return null;
    if (allowLocal && value.startsWith("/") && !value.startsWith("//") && !value.includes("\\")) return value;
    try {
      const url = new URL(value);
      if ((url.protocol === "https:" || url.protocol === "http:") && !url.username && !url.password) return url.href;
    } catch (_) { /* An invalid URL is displayed as text, never opened. */ }
    return null;
  }

  function link(text, value, local = false) {
    const url = safeUrl(value, local);
    if (!url) return null;
    const anchor = el("a", "", text);
    anchor.href = url;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    return anchor;
  }

  async function api(path, body) {
    const headers = { Accept: "application/json" };
    const options = { method: body === undefined ? "GET" : "POST", headers, credentials: "same-origin", cache: "no-store" };
    if (body !== undefined) {
      if (!state.bootstrap?.session_token) throw new Error("작업 세션에 연결하지 못했습니다. 상단에서 다시 연결해 주세요.");
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = state.bootstrap.session_token;
      options.body = JSON.stringify(body);
    }
    let response;
    try { response = await fetch(path, options); }
    catch (_) { throw new Error("로컬 서버에 연결할 수 없습니다. 프로그램이 실행 중인지 확인해 주세요."); }
    let result;
    try { result = await response.json(); }
    catch (_) { throw new Error("서버 응답을 읽지 못했습니다. 로컬 프로그램의 실행 상태를 확인해 주세요."); }
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : typeof result.error === "string" ? result.error : `요청을 처리하지 못했습니다. (${response.status})`);
    return result;
  }

  function errorAt(id, error) {
    put(id, typeof error === "string" ? error : error.message || "처리 중 오류가 발생했습니다.");
    hide(id, false);
  }

  function toast(message) {
    clearTimeout(state.toastTimer);
    put("toast-text", message);
    hide("toast", false);
    state.toastTimer = setTimeout(() => hide("toast"), 3500);
  }

  function warningsAt(id, warnings) {
    const target = $(id);
    target.replaceChildren();
    const messages = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
    for (const message of messages) target.append(el("p", "", typeof message === "string" ? message : message.message || String(message)));
    target.hidden = messages.length === 0;
  }

  function renderBootstrap() {
    const data = state.bootstrap;
    if (!data) return;
    put("daily-used", number(data.stats?.daily_used ?? 0));
    put("daily-limit", number(data.stats?.daily_limit ?? data.settings?.daily_limit));
    put("total-success", number(data.stats?.total_success ?? 0));
    const profileName = data.profile?.name || "전용 프로필";
    put("profile-summary", data.profile?.prepared ? profileName : "로그인 준비 필요");
    put("profile-name", profileName);
    put("profile-help", data.profile?.prepared ? "댓글 실행 전에 로그인용 Chrome 창을 직접 닫아 주세요. 실제 로그인 계정은 실행할 때 확인합니다." : "전용 Chrome에서 본인 계정에 로그인한 뒤, 열린 로그인 창을 직접 닫아 주세요.");
    put("app-version", data.version ? `맥락 스튜디오 v${data.version} · 로컬 과제 시연` : "로컬 과제 시연 도구");
    $("videos-refresh").disabled = state.loadingVideos;
    $("mode-live").disabled = false;
    renderAction();
  }

  async function refreshBootstrap(firstLoad = false) {
    hide("global-error");
    $("bootstrap-retry").disabled = true;
    try {
      state.bootstrap = await api("/api/bootstrap");
      renderBootstrap();
      if (firstLoad) await Promise.allSettled([loadVideos(), loadHistory()]);
      return true;
    } catch (error) {
      errorAt("global-error-text", error);
      hide("global-error", false);
      if (firstLoad) emptyVideos("서버와 연결하지 못했습니다.", "상단의 ‘다시 연결’을 눌러 주세요.", false);
      return false;
    } finally { $("bootstrap-retry").disabled = false; }
  }

  function emptyVideos(title, detail, loading) {
    const empty = el("div", "empty-state compact");
    if (loading) empty.append(el("span", "loader"));
    empty.append(el("p", "", title));
    if (detail) empty.append(el("p", "", detail));
    $("video-list").replaceChildren(empty);
    $("video-list").setAttribute("aria-busy", String(loading));
  }

  async function loadVideos() {
    if (!state.bootstrap) return;
    const request = ++state.videoRequest;
    const mode = state.mode;
    state.loadingVideos = true;
    $("videos-refresh").disabled = true;
    emptyVideos(mode === "sample" ? "연습 영상을 불러오고 있어요." : "YouTube 영상을 불러오고 있어요.", "", true);
    hide("video-warnings");
    try {
      const data = await api(`/api/videos?mode=${encodeURIComponent(mode)}`);
      if (request !== state.videoRequest || mode !== state.mode) return;
      state.videos = Array.isArray(data.videos) ? data.videos : [];
      warningsAt("video-warnings", data.warnings);
      populateChannelFilter();
      renderVideos();
    } catch (error) {
      if (request !== state.videoRequest) return;
      state.videos = [];
      put("video-count", "0");
      emptyVideos("영상을 불러오지 못했어요.", error.message, false);
    } finally {
      if (request === state.videoRequest) {
        state.loadingVideos = false;
        $("videos-refresh").disabled = false;
        $("video-list").setAttribute("aria-busy", "false");
      }
    }
  }

  function populateChannelFilter() {
    const current = $("channel-filter").value;
    const channels = new Map();
    for (const video of state.videos) if (video.channel_id) channels.set(String(video.channel_id), video.channel_title || "이름 없는 채널");
    const filter = $("channel-filter");
    filter.replaceChildren(new Option("모든 채널", ""));
    for (const [id, name] of channels) filter.add(new Option(name, id));
    if (channels.has(current)) filter.value = current;
  }

  function renderVideos() {
    const query = $("video-search").value.trim().toLocaleLowerCase("ko-KR");
    const channel = $("channel-filter").value;
    const videos = state.videos.filter((video) => (!channel || String(video.channel_id) === channel) && (!query || `${video.title || ""} ${video.channel_title || ""}`.toLocaleLowerCase("ko-KR").includes(query)));
    put("video-count", videos.length);
    if (!videos.length) {
      emptyVideos(state.videos.length ? "일치하는 영상이 없어요." : "아직 불러온 영상이 없어요.", state.videos.length ? "검색어나 채널을 바꿔 보세요." : state.mode === "live" ? "연결 설정을 확인하거나 영상 주소를 추가해 주세요." : "새로고침으로 다시 불러올 수 있어요.", false);
      return;
    }
    const fragment = document.createDocumentFragment();
    videos.forEach((video, index) => {
      const selected = state.selected?.id === video.id;
      const card = el("button", `video-card${selected ? " selected" : ""}`);
      card.type = "button";
      card.setAttribute("aria-pressed", String(selected));
      card.setAttribute("aria-label", `${video.title || "제목 없는 영상"}, ${video.channel_title || "채널 미상"}${selected ? ", 선택됨" : ""}`);
      const top = el("div", "video-card-top");
      const thumbnail = el("span", `video-thumbnail${state.mode === "sample" ? ` sample-${index % 3 + 1}` : ""}`);
      thumbnail.setAttribute("aria-hidden", "true");
      thumbnail.append(icon("play"));
      if (state.mode === "sample") {
        thumbnail.append(el("span", "sample-thumbnail-caption", "SAMPLE"), el("span", "sample-thumbnail-lines"));
      } else {
        const imageUrl = safeUrl(video.thumbnail);
        if (imageUrl) {
          const image = document.createElement("img");
          image.alt = "";
          image.loading = "lazy";
          image.referrerPolicy = "no-referrer";
          image.addEventListener("error", () => image.remove(), { once: true });
          image.src = imageUrl;
          thumbnail.append(image);
        }
      }
      const info = el("div", "video-card-info");
      info.append(el("span", "video-card-title", video.title || "제목 없는 영상"), el("p", "video-card-channel", video.channel_title || "채널 미상"));
      top.append(thumbnail, info);
      const bottom = el("div", "video-card-bottom");
      const tagText = state.mode === "sample" ? "가상 영상" : video.is_test ? "본인 테스트 영상" : "분석만 가능";
      bottom.append(el("span", `video-tag${video.is_test ? " test-tag" : ""}`, tagText));
      if (selected) {
        const selectedLabel = el("span", "selected-check");
        selectedLabel.append(icon("check"), document.createTextNode("선택됨"));
        bottom.append(selectedLabel);
      } else { bottom.append(el("span", "", dateText(video.published_at))); }
      card.append(top, bottom);
      card.addEventListener("click", () => selectVideo(video));
      fragment.append(card);
    });
    $("video-list").replaceChildren(fragment);
    $("video-list").setAttribute("aria-busy", "false");
  }

  function resetAnalysis() {
    state.analysisRequest += 1;
    state.validationRequest += 1;
    clearTimeout(state.validationTimer);
    state.analysis = null;
    state.validation = null;
    state.analyzing = false;
    state.validating = false;
    hide("analysis-result");
    hide("analysis-loading");
    hide("analysis-error");
    hide("validation-error");
    $("comment-input").value = "";
    $("comment-input").readOnly = false;
    hide("technical-test-panel", !(state.mode === "live" && state.selected?.is_test));
    $("technical-test-button").disabled = false;
    $("comment-checks").replaceChildren();
    $("transcript-input").value = "";
    $("transcript-details").open = false;
    $("analyze-button").disabled = false;
    put("analyze-button-label", "영상 분석하고 초안 만들기");
    put("analysis-status", state.selected ? "분석 준비" : "선택 대기");
    $("analysis-status").className = "status-label";
  }

  function selectVideo(video) {
    if (state.selected?.id === video.id) return;
    state.selected = video;
    resetAnalysis();
    hide("draft-empty");
    hide("draft-content", false);
    hide("transcript-details", state.mode === "sample");
    put("selected-title", video.title || "제목 없는 영상");
    put("selected-meta", `${video.channel_title || "채널 미상"} · ${dateText(video.published_at)}`);
    const sourceTag = $("selected-source");
    sourceTag.textContent = state.mode === "sample" ? "연습용 가상 영상" : video.is_test ? "본인 테스트 영상" : "실제 영상 · 분석만 가능";
    sourceTag.className = `source-tag${state.mode === "live" ? video.is_test ? " test" : " live" : ""}`;
    const url = state.mode === "live" ? safeUrl(video.url) : null;
    $("selected-link").hidden = !url;
    if (url) $("selected-link").href = url;
    else $("selected-link").removeAttribute("href");
    renderVideos();
    renderAction();
  }

  function setMode(mode) {
    if (mode === state.mode) return;
    state.mode = mode;
    state.selected = null;
    state.videos = [];
    resetAnalysis();
    $("video-search").value = "";
    $("channel-filter").replaceChildren(new Option("모든 채널", ""));
    $("mode-sample").classList.toggle("active", mode === "sample");
    $("mode-live").classList.toggle("active", mode === "live");
    $("mode-sample").setAttribute("aria-pressed", String(mode === "sample"));
    $("mode-live").setAttribute("aria-pressed", String(mode === "live"));
    $("mode-description").replaceChildren(el("span", "sample-dot"), document.createTextNode(mode === "sample" ? "가상 영상으로 흐름을 확인해요. YouTube에는 등록되지 않습니다." : "실제 영상의 내용을 분석합니다. 댓글 등록은 본인 테스트 영상에서만 가능합니다."));
    put("video-list-caption", mode === "sample" ? "연습용으로 만든 가상 영상입니다." : "YouTube API로 조회한 영상입니다. 검색 결과는 달라질 수 있어요.");
    hide("draft-content");
    hide("draft-empty", false);
    hide("resolve-panel", mode !== "live");
    hide("resolve-error");
    hide("live-profile", mode !== "live");
    hide("practice-link", mode !== "sample");
    put("publish-mode-tag", mode === "sample" ? "연습 모드" : "실제 모드");
    $("publish-mode-tag").className = `outline-tag${mode === "live" ? " live" : ""}`;
    put("publish-notice-title", mode === "sample" ? "먼저 편하게 연습해요." : "마지막 확인은 직접 해요.");
    put("publish-notice-text", mode === "sample" ? "로컬 연습 화면에 댓글을 입력하는 과정을 확인합니다. YouTube에는 등록되지 않아요." : "본인 테스트 영상과 로그인 계정을 확인한 뒤, 검토한 댓글 하나를 등록합니다.");
    put("execute-label", mode === "sample" ? "로컬 입력 연습" : "테스트 영상에 댓글 달기");
    put("publish-footnote", mode === "sample" ? "실행 전 준비된 초안을 다시 확인할 수 있어요." : "[과제 시연] 표시가 포함된 댓글만 등록합니다. 등록 후에는 실행 기록을 확인해 주세요.");
    renderAction();
    loadVideos();
  }

  async function resolveVideo(event) {
    event.preventDefault();
    hide("resolve-error");
    if (state.mode !== "live") return;
    const url = $("resolve-url").value.trim();
    if (!url) return;
    $("resolve-button").disabled = true;
    try {
      const result = await api("/api/videos/resolve", { url });
      if (state.mode !== "live") return;
      if (!result.video?.id) throw new Error("응답에 영상 정보가 없습니다.");
      state.videos = [result.video, ...state.videos.filter((video) => video.id !== result.video.id)];
      $("video-search").value = "";
      $("channel-filter").value = "";
      populateChannelFilter();
      selectVideo(result.video);
      $("resolve-url").value = "";
      toast("영상을 불러왔습니다. 내용을 분석해 보세요.");
    } catch (error) { errorAt("resolve-error", error); }
    finally { $("resolve-button").disabled = false; }
  }

  const sourceNames = {
    sample: "연습용 합성 데이터", fixture: "연습용 합성 데이터", synthetic: "연습용 합성 데이터",
    sample_transcript: "연습용 합성 자막", manual: "직접 입력한 내용", user_transcript: "직접 입력한 자막",
    manual_transcript: "직접 입력한 자막", transcript: "확보한 영상 자막", youtube_transcript: "YouTube 자막",
    captions: "영상 자막", metadata: "제목 · 설명", description: "영상 설명", title: "영상 제목", title_description: "제목 · 설명",
  };

  async function analyzeVideo(draftKind = "ai") {
    if (!state.selected || state.analyzing) return;
    const technical = draftKind === "technical_test";
    if (technical && !(state.mode === "live" && state.selected.is_test)) return;
    if (technical && $("transcript-input").value.trim()) {
      errorAt("analysis-error", "게시 테스트는 자막을 사용하지 않습니다. 직접 입력한 자막을 비운 뒤 실행하세요.");
      return;
    }
    const request = ++state.analysisRequest;
    const videoId = state.selected.id;
    const mode = state.mode;
    state.analyzing = true;
    state.analysis = null;
    state.validation = null;
    state.validating = false;
    state.validationRequest += 1;
    clearTimeout(state.validationTimer);
    hide("analysis-result");
    hide("analysis-error");
    hide("validation-error");
    hide("analysis-loading", false);
    $("analyze-button").disabled = true;
    $("technical-test-button").disabled = true;
    put("analyze-button-label", technical ? "테스트 초안 준비 중…" : "맥락 확인 중…");
    put("analysis-loading-title", technical ? "영상 제목과 테스트 범위를 확인하고 있어요." : "영상의 맥락을 살펴보고 있어요.");
    put("analysis-loading-text", technical ? "Gemini를 호출하지 않습니다. 아직 댓글을 게시하지 않습니다." : "확인 가능한 내용과 댓글에 쓸 근거를 정리합니다.");
    put("analysis-status", technical ? "테스트 준비 중" : "분석 중");
    $("analysis-status").className = "status-label is-working";
    renderAction();
    try {
      const payload = { video_id: videoId, mode, draft_kind: technical ? "technical_test" : "ai" };
      const transcript = $("transcript-input").value.trim();
      if (transcript && !technical) payload.transcript = transcript;
      const result = await api("/api/analyze", payload);
      if (request !== state.analysisRequest || videoId !== state.selected?.id || mode !== state.mode) return;
      if (!result.analysis_id || typeof result.comment !== "string") throw new Error("분석 응답에 초안 또는 분석 ID가 없습니다. 다시 분석해 주세요.");
      state.analysis = result;
      if (result.video && result.video.id === videoId) state.selected = { ...state.selected, ...result.video };
      renderAnalysis();
      await validateDraft(true);
    } catch (error) {
      if (request !== state.analysisRequest) return;
      errorAt("analysis-error", error);
      put("analysis-status", technical ? "테스트 준비 실패" : "분석 실패");
      $("analysis-status").className = "status-label is-error";
    } finally {
      if (request === state.analysisRequest) {
        state.analyzing = false;
        hide("analysis-loading");
        $("analyze-button").disabled = false;
        $("technical-test-button").disabled = false;
        put("analyze-button-label", state.analysis && state.analysis.source_mode !== "technical_test" ? "다시 분석하고 초안 만들기" : "영상 분석하고 초안 만들기");
        renderAction();
      }
    }
  }

  function renderAnalysis() {
    const analysis = state.analysis;
    if (!analysis) return;
    put("worry-text", analysis.worry || "확인 가능한 내용에 맞춰 댓글의 초점을 잡았습니다.");
    put("evidence-quote", analysis.evidence_quote || "인용할 수 있는 내용이 제공되지 않았습니다.");
    const source = analysis.evidence_source || analysis.source_mode || "출처 정보 없음";
    put("evidence-source", analysis.source_mode === "sample" ? "연습용 합성 자막" : sourceNames[source] || source);
    let timestamp = analysis.evidence_timestamp;
    if (typeof timestamp === "number") timestamp = `${Math.floor(timestamp / 60)}:${String(Math.floor(timestamp % 60)).padStart(2, "0")}`;
    put("evidence-timestamp", timestamp ? `근거 시점 · ${timestamp}` : "");
    hide("evidence-timestamp", !timestamp);
    warningsAt("analysis-warnings", analysis.warnings);
    $("comment-input").value = analysis.comment;
    const technical = analysis.source_mode === "technical_test";
    $("comment-input").readOnly = technical;
    put("comment-guidance", technical ? "게시 테스트 전용 고정 문구입니다. AI 생성 결과가 아닙니다." : "직접 읽고, 내 말로 다듬어 주세요.");
    hide("analysis-result", false);
    hide("analysis-loading");
    const draftTypes = { sample: "연습용 고정 초안", template: "규칙 기반 초안", ai: "AI 초안", technical_test: "게시 테스트 · AI 아님" };
    put("analysis-status", draftTypes[analysis.source_mode] || "생성 방식 확인 필요");
    $("analysis-status").className = "status-label is-success";
    updateCharacterCount();
    renderChecks(Array.isArray(analysis.checks) ? analysis.checks : []);
  }

  function updateCharacterCount() {
    const count = Array.from($("comment-input").value).length;
    $("comment-count").replaceChildren(document.createTextNode(`${count} `), el("span", "", "/ 40–120자"));
    $("comment-count").classList.toggle("invalid", count < 40 || count > 120);
    return count;
  }

  function renderChecks(checks, pending = false) {
    const target = $("comment-checks");
    const fragment = document.createDocumentFragment();
    for (const check of checks) {
      const pass = check.ok === true;
      const item = el("span", `check-item${pass ? "" : " failed"}`, "");
      item.append(icon(pass ? "check" : "cross"), document.createTextNode(check.name || "내용 검증"));
      if (check.detail) item.title = String(check.detail);
      if (!pass && check.detail) item.setAttribute("aria-label", `${check.name || "내용 검증"}: ${check.detail}`);
      fragment.append(item);
    }
    if (pending) fragment.append(el("span", "check-item pending", "변경한 내용을 확인하고 있어요…"));
    target.replaceChildren(fragment);
  }

  function draftChanged() {
    updateCharacterCount();
    state.validation = null;
    state.validating = true;
    state.validationRequest += 1;
    hide("validation-error");
    clearTimeout(state.validationTimer);
    renderChecks([], true);
    renderAction();
    state.validationTimer = setTimeout(() => validateDraft(), 450);
  }

  async function validateDraft() {
    if (!state.analysis) return false;
    const analysisId = state.analysis.analysis_id;
    const comment = $("comment-input").value;
    const request = ++state.validationRequest;
    state.validating = true;
    renderAction();
    try {
      const result = await api("/api/validate", { analysis_id: analysisId, comment });
      if (request !== state.validationRequest || analysisId !== state.analysis?.analysis_id || comment !== $("comment-input").value) return false;
      state.validation = { ...result, comment };
      renderChecks(Array.isArray(result.checks) ? result.checks : []);
      hide("validation-error");
      return result.valid === true;
    } catch (error) {
      if (request !== state.validationRequest) return false;
      state.validation = null;
      errorAt("validation-error", error);
      renderChecks([]);
      return false;
    } finally {
      if (request === state.validationRequest) {
        state.validating = false;
        renderAction();
      }
    }
  }

  function actionReason() {
    if (!state.bootstrap) return "로컬 서버 연결을 확인해 주세요.";
    if (state.submitting) return "실행을 시작하고 있어요. 잠시 기다려 주세요.";
    if (state.job && !TERMINAL.has(state.job.status)) return "현재 실행을 마친 뒤 다음 댓글을 준비할 수 있어요.";
    if (state.job?.status === "unknown" && state.mode === "live") return "이전 등록 결과를 확인하지 못했습니다. YouTube에서 실제 등록 여부를 먼저 확인해 주세요.";
    if (!state.selected) return "영상과 댓글 초안을 준비하면 시작할 수 있어요.";
    if (state.mode === "live" && state.job?.mode === "live" && state.job?.status === "success" && state.job.video_id === state.selected.id) return "이 영상은 이미 댓글 등록을 확인했습니다. 같은 영상에는 다시 게시하지 않습니다.";
    if (state.analyzing) return "영상 분석이 끝나면 댓글 초안을 확인해 주세요.";
    if (!state.analysis) return "먼저 영상을 분석하고 댓글 초안을 만들어 주세요.";
    if (state.validating) return "수정한 댓글이 등록 조건에 맞는지 확인하고 있어요.";
    if (!state.validation || state.validation.comment !== $("comment-input").value) return "댓글 검증을 완료하지 못했습니다. 내용을 수정해 다시 확인해 주세요.";
    if (!state.validation.valid) return "댓글 아래의 확인 항목을 살펴보고 초안을 다듬어 주세요.";
    if (state.mode === "live") {
      if (!state.selected.is_test) return "이 영상은 분석만 가능합니다. 설정에 등록한 본인 테스트 영상을 선택해 주세요.";
      if (!state.bootstrap.readiness?.test_video) return "연결 설정에서 본인 테스트 영상과 채널 ID를 확인해 주세요.";
      if (!state.bootstrap.readiness?.chrome) return "Chrome 실행 환경을 확인해 주세요. 설정 및 실행 안내를 참고할 수 있어요.";
      if (!state.bootstrap.profile?.prepared) return "전용 브라우저를 열어 본인 계정에 로그인해 주세요.";
      const used = Number(state.bootstrap.stats?.daily_used || 0);
      const limit = Number(state.bootstrap.stats?.daily_limit ?? state.bootstrap.settings?.daily_limit);
      if (Number.isFinite(limit) && used >= limit) return "오늘의 실제 등록 한도에 도달했습니다. 실행 기록을 확인해 주세요.";
      if (!$("comment-input").value.includes("[과제 시연]")) return "실제 등록할 댓글에는 [과제 시연] 표시가 필요합니다.";
    }
    return null;
  }

  function renderAction() {
    put("execution-video", state.selected?.title || "선택 전");
    $("execution-video").title = state.selected?.title || "";
    put("execution-draft", state.analyzing ? "초안 생성 중" : state.validating ? "조건 확인 중" : state.validation?.valid ? "검토 후 실행 가능" : state.analysis ? "내용 확인 필요" : "초안 대기");
    put("execution-target", state.mode === "sample" ? "로컬 연습 화면" : state.selected?.is_test ? "본인 YouTube 테스트 영상" : "테스트 영상 선택 필요");
    const reason = actionReason();
    put("action-blocked-reason", reason || (state.mode === "sample" ? "준비됐어요. 댓글을 확인하고 로컬 입력을 연습해 보세요." : "준비됐어요. 영상·계정·댓글을 마지막으로 확인해 주세요."));
    $("action-blocked-reason").classList.toggle("ready", !reason);
    $("execute-button").disabled = Boolean(reason);
    const executing = state.submitting || (state.job && !TERMINAL.has(state.job.status));
    $("mode-sample").disabled = Boolean(executing);
    $("mode-live").disabled = Boolean(executing) || !state.bootstrap;
    $("analyze-button").disabled = state.analyzing || Boolean(executing);
    $("technical-test-button").disabled = state.analyzing || Boolean(executing);
    $("comment-input").disabled = Boolean(executing);
    $("settings-open").disabled = Boolean(executing);
    $("profile-open").disabled = Boolean(executing);
    $("profile-close").disabled = Boolean(executing);
    $("profile-refresh").disabled = Boolean(executing);
  }

  async function copyComment() {
    const comment = $("comment-input").value;
    if (!comment) return;
    try {
      await navigator.clipboard.writeText(comment);
      toast("댓글 초안을 복사했습니다.");
    } catch (_) {
      $("comment-input").focus();
      $("comment-input").select();
      toast("댓글을 선택했습니다. Ctrl+C 또는 ⌘C로 복사해 주세요.");
    }
  }

  function openSettings() {
    const settings = state.bootstrap?.settings || {};
    $("youtube-api-key").value = "";
    $("ai-api-key").value = "";
    $("youtube-api-key").placeholder = settings.youtube_api_key_set ? "등록됨 · 변경할 때만 입력" : "YouTube Data API 키 입력";
    $("ai-api-key").placeholder = settings.ai_api_key_set ? "등록됨 · 변경할 때만 입력" : "선택한 제공자의 API 키 입력";
    put("youtube-key-status", settings.youtube_api_key_set ? "등록됨" : "미등록");
    put("ai-key-status", settings.ai_api_key_set ? "등록됨" : "미등록");
    $("youtube-key-status").classList.toggle("ready", Boolean(settings.youtube_api_key_set));
    $("ai-key-status").classList.toggle("ready", Boolean(settings.ai_api_key_set));
    $("ai-provider").value = settings.ai_provider || "gemini";
    $("ai-model").value = settings.ai_model || ($("ai-provider").value === "gemini" ? "gemini-3.8-flash" : "");
    $("ai-model").placeholder = $("ai-provider").value === "gemini" ? "gemini-3.8-flash" : "사용 가능한 OpenAI 모델 ID";
    $("test-video-url").value = settings.test_video_url || "";
    $("test-channel-id").value = settings.test_channel_id || "";
    $("expected-handle").value = settings.expected_handle || "";
    $("daily-limit-input").value = settings.daily_limit ?? 3;
    hide("settings-error");
    $("settings-dialog").showModal();
  }

  function closeSettings() {
    if (state.settingsSaving) return;
    $("settings-dialog").close();
    $("youtube-api-key").value = "";
    $("ai-api-key").value = "";
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (state.settingsSaving) return;
    hide("settings-error");
    const payload = {
      ai_provider: $("ai-provider").value, ai_model: $("ai-model").value.trim(),
      test_video_url: $("test-video-url").value.trim(), test_channel_id: $("test-channel-id").value.trim(),
      expected_handle: $("expected-handle").value.trim(), daily_limit: Number($("daily-limit-input").value),
    };
    if (!Number.isInteger(payload.daily_limit) || payload.daily_limit < 1 || payload.daily_limit > 10) { errorAt("settings-error", "하루 등록 한도는 1부터 10 사이의 정수로 입력해 주세요."); return; }
    const youtubeKey = $("youtube-api-key").value.trim();
    const aiKey = $("ai-api-key").value.trim();
    if (youtubeKey) payload.youtube_api_key = youtubeKey;
    if (aiKey) payload.ai_api_key = aiKey;
    state.settingsSaving = true;
    $("settings-save").disabled = true;
    $("settings-cancel").disabled = true;
    $("settings-close").disabled = true;
    put("settings-save", "저장 중…");
    try {
      await api("/api/settings", payload);
      const ready = await refreshBootstrap();
      state.settingsSaving = false;
      closeSettings();
      toast(ready ? "연결 설정을 저장했습니다." : "설정을 저장했습니다. 서버 연결을 다시 확인해 주세요.");
      if (ready && state.mode === "live") {
        state.selected = null;
        resetAnalysis();
        hide("draft-content");
        hide("draft-empty", false);
        renderAction();
        loadVideos();
      }
    } catch (error) { errorAt("settings-error", error); }
    finally {
      state.settingsSaving = false;
      $("settings-save").disabled = false;
      $("settings-cancel").disabled = false;
      $("settings-close").disabled = false;
      put("settings-save", "설정 저장");
    }
  }

  async function profileAction(action) {
    hide("profile-message");
    $("profile-open").disabled = true;
    $("profile-close").disabled = true;
    try {
      const result = await api(`/api/profile/${action}`, {});
      put("profile-message", result.message || (action === "open" ? "전용 브라우저를 열었습니다." : "전용 브라우저를 닫았습니다."));
      $("profile-message").className = "profile-message";
      hide("profile-message", false);
      await refreshBootstrap();
    } catch (error) {
      errorAt("profile-message", error);
      $("profile-message").className = "profile-message field-error";
    } finally { renderAction(); }
  }

  function openConfirmation() {
    if (actionReason()) return;
    const sample = state.mode === "sample";
    state.confirmSnapshot = { analysis_id: state.analysis.analysis_id, comment: $("comment-input").value, mode: state.mode, video_id: state.selected.id };
    put("confirm-title", sample ? "이 댓글로 연습할까요?" : "이 댓글을 실제로 등록할까요?");
    put("confirm-eyebrow", sample ? "LOCAL REHEARSAL" : "FINAL CHECK · YOUTUBE");
    put("confirm-description", sample ? "아래 내용으로 로컬 연습 화면에 댓글을 입력합니다. 실제 YouTube에는 댓글이 등록되지 않습니다." : "확인한 댓글 1개를 본인 테스트 영상에 등록합니다. 실행 후 실제 등록 결과를 기록에서 확인해 주세요.");
    put("confirm-video", state.selected.title);
    put("confirm-target", sample ? "내 컴퓨터의 로컬 연습 화면" : state.selected.url || "설정된 본인 YouTube 테스트 영상");
    put("confirm-profile", sample ? "로컬 연습 · YouTube 계정 사용 안 함" : `${state.bootstrap.profile?.name || "전용 프로필"}${state.bootstrap.settings?.expected_handle ? ` · ${state.bootstrap.settings.expected_handle}` : ""}`);
    put("confirm-comment-text", state.confirmSnapshot.comment);
    put("confirm-checkbox-label", sample ? "연습할 영상과 댓글 내용을 확인했습니다." : "본인 테스트 영상, 계정과 댓글 전문을 확인했습니다.");
    put("confirm-submit", sample ? "이 내용으로 연습" : "이 내용으로 댓글 등록");
    $("confirm-checkbox").checked = false;
    $("confirm-submit").disabled = true;
    hide("confirm-error");
    $("confirm-dialog").showModal();
  }

  function closeConfirmation() {
    if (state.submitting) return;
    $("confirm-dialog").close();
    state.confirmSnapshot = null;
  }

  async function executeConfirmed() {
    const snapshot = state.confirmSnapshot;
    if (!snapshot || !$("confirm-checkbox").checked || state.submitting) return;
    if (snapshot.analysis_id !== state.analysis?.analysis_id || snapshot.comment !== $("comment-input").value || snapshot.mode !== state.mode || snapshot.video_id !== state.selected?.id) {
      errorAt("confirm-error", "준비한 내용이 바뀌었습니다. 창을 닫고 다시 확인해 주세요.");
      return;
    }
    const reason = actionReason();
    if (reason) { errorAt("confirm-error", reason); return; }
    state.submitting = true;
    $("confirm-submit").disabled = true;
    $("confirm-cancel").disabled = true;
    $("confirm-close").disabled = true;
    $("confirm-checkbox").disabled = true;
    put("confirm-submit", "실행 시작 중…");
    renderAction();
    try {
      const payload = { analysis_id: snapshot.analysis_id, comment: snapshot.comment };
      if (snapshot.mode === "live") payload.confirmed = true;
      const result = await api(snapshot.mode === "sample" ? "/api/rehearse" : "/api/publish", payload);
      if (!result.job_id) throw new Error("실행 ID를 확인하지 못했습니다. 실행 기록에서 결과를 확인해 주세요.");
      state.job = { id: result.job_id, status: "queued", stage: "실행 준비 중", events: [], mode: snapshot.mode };
      state.jobPollingError = false;
      $("confirm-dialog").close();
      state.confirmSnapshot = null;
      renderJob();
      pollJob(result.job_id);
    } catch (error) {
      errorAt("confirm-error", error);
      put("confirm-submit", snapshot.mode === "sample" ? "이 내용으로 연습" : "이 내용으로 댓글 등록");
    } finally {
      state.submitting = false;
      $("confirm-submit").disabled = !$("confirm-checkbox").checked;
      $("confirm-cancel").disabled = false;
      $("confirm-close").disabled = false;
      $("confirm-checkbox").disabled = false;
      renderAction();
    }
  }

  const statusNames = { queued: "대기 중", pending: "대기 중", running: "진행 중", success: "등록 확인", failed: "실패", unknown: "결과 미확인", rehearsed: "연습 완료" };
  const stageNames = {
    queued: "실행 순서를 기다리고 있어요.", starting: "브라우저를 준비하고 있어요.",
    browser: "전용 브라우저를 열고 있어요.", opening: "대상 화면을 열고 있어요.",
    navigating: "선택한 영상으로 이동하고 있어요.", verifying: "영상과 계정을 확인하고 있어요.",
    typing: "확인한 댓글을 입력하고 있어요.", submitting: "댓글 등록을 요청하고 있어요.",
    checking: "등록 결과를 확인하고 있어요.", completed: "실행을 마쳤습니다.",
    done: "실행을 마쳤습니다.", failed: "실행이 중단되었습니다.",
  };

  function renderJob() {
    const job = state.job;
    if (!job) { hide("job-panel"); return; }
    hide("job-panel", false);
    put("job-status", state.jobPollingError ? "조회 연결 끊김" : statusNames[job.status] || job.status);
    $("job-status").className = `status-label${["success", "rehearsed"].includes(job.status) ? " is-success" : ["failed", "unknown"].includes(job.status) || state.jobPollingError ? " is-error" : " is-working"}`;
    $("job-progress").className = `job-progress${["success", "rehearsed"].includes(job.status) ? " complete" : ["failed", "unknown"].includes(job.status) || state.jobPollingError ? " failed" : ""}`;
    let stage = stageNames[job.stage] || job.stage || "실행 상태를 확인하고 있어요.";
    if (job.status === "rehearsed") stage = "로컬 입력 연습을 마쳤습니다. YouTube에는 등록하지 않았습니다.";
    if (job.status === "success") stage = "YouTube 댓글 등록을 확인했습니다. 아래 결과 링크를 살펴보세요.";
    if (job.status === "unknown") stage = "등록 결과를 확정하지 못했습니다. 중복 등록을 피하려면 YouTube에서 직접 확인해 주세요.";
    if (state.jobPollingError) stage = "실행 상태 조회가 끊겼습니다. 작업이 계속 진행될 수 있으므로 상태를 다시 확인해 주세요.";
    put("job-stage", stage);
    const events = document.createDocumentFragment();
    for (const event of Array.isArray(job.events) ? job.events : []) {
      const item = el("li", "");
      const time = el("time", "", timeText(event.time));
      if (event.time) time.dateTime = String(event.time);
      item.append(time, el("span", "", event.message || ""));
      events.append(item);
    }
    $("job-events").replaceChildren(events);
    $("job-events").scrollTop = $("job-events").scrollHeight;
    if (job.error) errorAt("job-error", job.error);
    else hide("job-error");
    const links = document.createDocumentFragment();
    const screenshot = link("실행 화면 보기 ↗", job.screenshot_url, true);
    const comment = link("등록 댓글 보기 ↗", job.comment_url, false);
    if (screenshot) links.append(screenshot);
    if (comment) links.append(comment);
    $("job-links").replaceChildren(links);
    hide("job-recheck", !state.jobPollingError && job.status !== "unknown");
    const canVerify = job.mode === "live" && job.status === "unknown" && Boolean(job.post_started);
    hide("job-verify-existing", !canVerify);
    $("job-verify-existing").disabled = Boolean(state.submitting);
    const proof = job.verification;
    hide("verification-details", !proof);
    if (proof?.method === "youtube_api_double_read") {
      put("job-status", "등록 확인 · API");
      put("job-stage", job.stage || "기존 댓글을 API로 재조회해 확인했습니다. 새 댓글은 게시하지 않았습니다.");
      put("verification-details", `확인 방식: YouTube API 이중 조회 · ${dateText(proof.verified_at, true)}. 영상·작성자 채널 ID·댓글 전문·동일 댓글 ID를 대조했습니다. 브라우저 화면 자동 대조 성공과는 구분합니다.`);
    }
    renderAction();
  }

  async function pollJob(id) {
    clearTimeout(state.pollTimer);
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(id)}`);
      if (state.job?.id !== id) return;
      state.job = { ...state.job, ...result };
      state.jobPollingError = false;
      renderJob();
      if (TERMINAL.has(result.status)) {
        await Promise.allSettled([loadHistory(), refreshBootstrap()]);
        if (result.status === "rehearsed") toast("로컬 입력 연습을 마쳤습니다.");
        else if (result.status === "success") toast("실제 댓글 등록을 확인했습니다.");
      } else { state.pollTimer = setTimeout(() => pollJob(id), 1300); }
    } catch (_) {
      if (state.job?.id !== id) return;
      state.jobPollingError = true;
      renderJob();
    }
  }

  async function verifyExisting(item) {
    if (!item?.id || state.submitting || (state.job && !TERMINAL.has(state.job.status))) return;
    if (!window.confirm(`기존 댓글만 읽어서 재확인합니다. 새 댓글을 등록하거나 기존 댓글을 수정·삭제하지 않습니다.\n\n영상: ${item.video_title || "기존 본인 테스트 영상"}\n\n${item.comment || ""}\n\nYouTube API로 재확인할까요?`)) return;
    state.submitting = true;
    for (const button of document.querySelectorAll('[data-verify-existing]')) button.disabled = true;
    renderAction();
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(item.id)}/verify-existing`, {confirmed: true});
      state.job = await api(`/api/jobs/${encodeURIComponent(result.job_id)}`);
      state.jobPollingError = false;
      renderJob();
      $("job-panel").scrollIntoView({block: "nearest", behavior: "smooth"});
      toast("기존 댓글을 읽기 전용으로 재확인합니다. 새 게시 없음.");
      pollJob(result.job_id);
    } catch (error) {
      toast(error.message);
      errorAt("global-error-text", error);
      hide("global-error", false);
    } finally {
      state.submitting = false;
      $("job-verify-existing").disabled = false;
      renderAction();
      await loadHistory();
    }
  }

  async function loadHistory() {
    const request = ++state.historyRequest;
    $("history-refresh").disabled = true;
    try {
      const result = await api("/api/history");
      if (request !== state.historyRequest) return;
      const items = Array.isArray(result.items) ? result.items : [];
      put("history-count", items.length);
      // Restore the most relevant old run after a server/browser restart.
      // This GET-only restoration does not automatically start verification.
      if (!state.job) {
        const outstanding = items.find(j => !TERMINAL.has(j.status)) || items.find(j => j.status === "unknown" && j.post_started);
        if (outstanding) {
          state.job = outstanding;
          renderJob();
          if (!TERMINAL.has(outstanding.status)) pollJob(outstanding.id);
        }
      }
      if (!items.length) {
        $("history-content").replaceChildren(el("p", "history-empty", "아직 실행한 기록이 없습니다. 영상 하나를 선택해 시작해 보세요."));
        return;
      }
      const table = el("table", "history-table");
      const caption = el("caption", "sr-only", "실행 시각, 모드, 영상, 댓글, 결과와 증빙 링크");
      table.append(caption);
      const head = document.createElement("thead");
      const headRow = document.createElement("tr");
      for (const title of ["실행 시각 · 한국시간", "모드", "영상", "댓글", "결과", "확인"]) {
        const th = el("th", "", title); th.scope = "col"; headRow.append(th);
      }
      head.append(headRow); table.append(head);
      const body = document.createElement("tbody");
      for (const item of items) {
        const row = document.createElement("tr");
        row.append(el("td", "", dateText(item.created_at, true)));
        const modeCell = document.createElement("td");
        const sample = item.mode === "sample" || item.mode === "rehearsal" || item.status === "rehearsed";
        modeCell.append(el("span", `history-mode${sample ? "" : " live"}`, sample ? "로컬 연습" : "실제 YouTube"));
        row.append(modeCell);
        const videoCell = document.createElement("td");
        videoCell.append(el("p", "history-video-title", item.video_title || "영상 제목 없음"));
        if (item.profile) videoCell.append(el("p", "history-profile", item.profile));
        row.append(videoCell);
        const commentCell = document.createElement("td");
        commentCell.append(el("p", "history-comment", item.comment || "—"));
        if (item.error) commentCell.append(el("p", "history-error", item.error));
        row.append(commentCell);
        const statusCell = document.createElement("td");
        const safeClass = ["success", "failed", "unknown", "rehearsed", "running", "queued"].includes(item.status) ? item.status : "";
        statusCell.append(el("span", `history-status ${safeClass}`, item.verification?.method === "youtube_api_double_read" && item.status === "success" ? "등록 확인 · API" : statusNames[item.status] || item.status || "정보 없음"));
        row.append(statusCell);
        const linkCell = document.createElement("td");
        const commentLink = link("댓글 ↗", item.comment_url);
        const screenshotLink = link("실행 화면 ↗", item.screenshot_url, true);
        if (commentLink) linkCell.append(commentLink);
        if (screenshotLink) linkCell.append(screenshotLink);
        if (!commentLink && !screenshotLink) linkCell.textContent = "—";
        if (item.mode === "live" && item.status === "unknown" && item.post_started) {
          if (linkCell.textContent === "—") linkCell.textContent = "";
          const verifyButton = el("button", "text-button", "기존 댓글 재확인");
          verifyButton.type = "button";
          verifyButton.dataset.verifyExisting = item.id;
          verifyButton.title = "새 댓글을 게시하지 않고 기존 댓글만 조회합니다.";
          verifyButton.disabled = Boolean(state.submitting || (state.job && !TERMINAL.has(state.job.status)));
          verifyButton.addEventListener("click", () => verifyExisting(item));
          linkCell.append(verifyButton);
        }
        const details = el("button", "text-button", "기록 보기");
        details.type = "button";
        details.addEventListener("click", () => {
          if (state.job && !TERMINAL.has(state.job.status) && state.job.id !== item.id) return;
          state.job = item; renderJob(); pollJob(item.id);
          $("job-panel").scrollIntoView({block: "nearest", behavior: "smooth"});
        });
        linkCell.append(details);
        row.append(linkCell); body.append(row);
      }
      table.append(body);
      $("history-content").replaceChildren(table);
    } catch (error) {
      if (request === state.historyRequest) $("history-content").replaceChildren(el("p", "history-empty", `실행 기록을 불러오지 못했습니다. ${error.message}`));
    } finally { if (request === state.historyRequest) $("history-refresh").disabled = false; }
  }

  $("mode-sample").addEventListener("click", () => setMode("sample"));
  $("mode-live").addEventListener("click", () => setMode("live"));
  $("videos-refresh").addEventListener("click", loadVideos);
  $("video-search").addEventListener("input", () => { if (!state.loadingVideos) renderVideos(); });
  $("channel-filter").addEventListener("change", () => { if (!state.loadingVideos) renderVideos(); });
  $("resolve-form").addEventListener("submit", resolveVideo);
  $("analyze-button").addEventListener("click", () => analyzeVideo("ai"));
  $("technical-test-button").addEventListener("click", () => analyzeVideo("technical_test"));
  $("comment-input").addEventListener("input", draftChanged);
  $("comment-copy").addEventListener("click", copyComment);
  $("settings-open").addEventListener("click", openSettings);
  $("settings-close").addEventListener("click", closeSettings);
  $("settings-cancel").addEventListener("click", closeSettings);
  $("settings-form").addEventListener("submit", saveSettings);
  $("settings-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeSettings(); });
  $("profile-open").addEventListener("click", () => profileAction("open"));
  $("profile-close").addEventListener("click", () => profileAction("close"));
  $("profile-refresh").addEventListener("click", async () => {
    $("profile-refresh").disabled = true;
    const ready = await refreshBootstrap();
    if (ready) {
      put("profile-message", state.bootstrap.profile?.prepared ? "프로필 파일 준비를 확인했습니다. 실제 로그인 계정은 댓글 실행 시 다시 확인합니다." : "프로필이 아직 준비되지 않았습니다. 전용 브라우저를 열고 준비를 마친 뒤 다시 확인해 주세요.");
      $("profile-message").className = "profile-message";
      hide("profile-message", false);
    }
    renderAction();
  });
  $("execute-button").addEventListener("click", openConfirmation);
  $("confirm-close").addEventListener("click", closeConfirmation);
  $("confirm-cancel").addEventListener("click", closeConfirmation);
  $("confirm-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeConfirmation(); });
  $("confirm-checkbox").addEventListener("change", () => { $("confirm-submit").disabled = !$("confirm-checkbox").checked; });
  $("confirm-submit").addEventListener("click", executeConfirmed);
  $("job-verify-existing").addEventListener("click", () => verifyExisting(state.job));
  $("job-recheck").addEventListener("click", () => { if (state.job?.id) pollJob(state.job.id); });
  $("history-refresh").addEventListener("click", loadHistory);
  $("bootstrap-retry").addEventListener("click", () => refreshBootstrap(true));
  $("ai-provider").addEventListener("change", () => {
    const provider = $("ai-provider").value;
    const configured = state.bootstrap?.settings || {};
    $("ai-model").placeholder = provider === "gemini" ? "gemini-3.8-flash" : "사용 가능한 OpenAI 모델 ID";
    $("ai-model").value = provider === configured.ai_provider ? configured.ai_model || "" : provider === "gemini" ? "gemini-3.8-flash" : "";
    if (configured.ai_api_key_set && provider !== configured.ai_provider) {
      put("ai-key-status", "제공자 변경 · 키 확인 필요");
      $("ai-key-status").classList.remove("ready");
    } else {
      put("ai-key-status", configured.ai_api_key_set ? "등록됨" : "미등록");
      $("ai-key-status").classList.toggle("ready", Boolean(configured.ai_api_key_set));
    }
  });
  window.addEventListener("beforeunload", () => { clearTimeout(state.pollTimer); clearTimeout(state.validationTimer); });
  let lastFocusRefresh = 0;
  window.addEventListener("focus", () => {
    if (state.mode === "live" && state.bootstrap && !state.submitting && (!state.job || TERMINAL.has(state.job.status)) && Date.now() - lastFocusRefresh > 3000) {
      lastFocusRefresh = Date.now();
      refreshBootstrap();
    }
  });

  renderAction();
  refreshBootstrap(true);
})();
