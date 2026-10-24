"use strict";
/* 設定ダッシュボードの「ナレッジ」画面（インスタンス詳細のサブタブ）。
 * ベクターDB（src/vector_db/）のコレクションの作成・編集・削除と、文書の取り込み。
 * API は admin/knowledge.py。共通ヘルパー（api / el / dataTable / fmtTime /
 * startMonitorPolling 等）は admin.js のものを使う。
 */

function knowledgePath(name, suffix) {
  return `/api/instances/${encodeURIComponent(name)}/knowledge${suffix || ""}`;
}

const KNOWLEDGE_JOB_LABELS = { running: "取り込み中", done: "完了", error: "エラー", stopped: "停止" };
// 自動取り込みの曜日（catalog.sqlite の schedule_weekdays。0=月〜6=日、Python の weekday() と同じ）。
const KNOWLEDGE_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"];

async function knowledgeAction(fn) {
  try {
    return await fn();
  } catch (e) {
    alert(e.message);
    return null;
  }
}

async function renderKnowledgePanel(container, name) {
  stopMonitorPolling();
  container.innerHTML = "";
  const data = await api(knowledgePath(name));

  const settings = [
    `機能: ${data.enabled ? "有効" : "無効"}`,
    `保存先: ${data.backend === "qdrant" ? `Qdrant（${data.qdrant_url || "URL 未設定"}、接頭辞 ${data.qdrant_collection_prefix}、プリセット ${data.qdrant_preset}）` : `sqlite-vec（${data.dir}）`}、距離関数 ${data.distance}`,
    `埋め込みモデル: ${data.embedding_model || "未設定"}`,
    `接頭辞: 検索語 ${JSON.stringify(data.query_prefix)}・文書 ${JSON.stringify(data.document_prefix)}`,
    `対象の拡張子: ${data.include_extensions.join(" ")}`,
    `埋め込み方: ${data.image_input ? "本文＋画像（image_input = true。PDF・Office はページ画像も、画像ファイルは画像で埋め込む）" : "テキスト"}`,
  ];
  container.append(el("h3", { text: "ナレッジ（社内文書の意味検索）" }));
  container.append(el("p", { class: "hint", text: settings.join(" / ") }));
  if (!data.enabled) {
    container.append(
      el("p", {
        class: "error",
        text: "このインスタンスでは [vector_db] enabled が false です。config.ini タブで enabled・embedding_url（Qdrant を使う場合は backend・qdrant_url も）を設定してから取り込んでください。コレクションの登録だけは先にできます。",
      })
    );
  }

  const list = el("div");
  container.append(list);
  container.append(buildCreateCollectionForm(container, name));

  const draw = (collections) => {
    list.innerHTML = "";
    const rows = collections.map((c) => [
      el("div", {}, [el("strong", { text: c.name }), c.description ? el("div", { class: "hint", text: c.description }) : null]),
      el("div", {}, [c.source_folder, el("div", { class: "hint", text: c.recursive ? "サブフォルダも含む" : "直下のみ" })]),
      `${c.file_count} 件 / ${c.chunk_count} チャンク`,
      c.model ? `${c.model}（${c.dim} 次元）` : "-",
      fmtTime(c.last_ingested_at),
      knowledgeScheduleCell(c, data.enabled),
      knowledgeJobCell(c.job, data.max_file_mb),
      knowledgeActions(container, name, c, data.enabled),
    ]);
    list.append(
      dataTable(["コレクション", "取り込み元フォルダ", "文書", "埋め込みモデル", "最終取り込み", "自動取り込み", "状態", "操作"], rows, "まだコレクションはありません。")
    );
  };
  draw(data.collections);

  // 取り込み中のジョブがある間（と、終わった直後の1回）だけ表示を更新する
  // （編集中の追加フォームを描き直さないよう、一覧の表だけを差し替える）。
  const hasRunning = (collections) => collections.some((c) => c.job && c.job.state === "running");
  let wasRunning = hasRunning(data.collections);
  startMonitorPolling(container, async () => {
    if (!wasRunning) return;
    const latest = await api(knowledgePath(name));
    wasRunning = hasRunning(latest.collections);
    draw(latest.collections);
  });
}

function knowledgeScheduleText(c) {
  const days = c.schedule_weekdays.length ? c.schedule_weekdays.map((d) => KNOWLEDGE_WEEKDAYS[d]).join("・") : "毎日";
  return `${days} ${c.schedule_times.join(", ")}`;
}

function knowledgeScheduleCell(c, enabled) {
  if (!c.schedule_enabled) return "なし";
  const parts = [el("div", { text: knowledgeScheduleText(c) })];
  if (!enabled) parts.push(el("div", { class: "hint", text: "[vector_db] enabled が false のため実行しません" }));
  else if (c.next_scheduled_at) parts.push(el("div", { class: "hint", text: `次回 ${fmtTime(c.next_scheduled_at)}` }));
  return el("div", {}, parts);
}

function knowledgeJobCell(job, maxFileMb) {
  if (!job) return "-";
  // 停止を求めた後は、処理中のファイルが終わるまで running のままのため「停止中」と出す。
  const label = job.state === "running" && job.stop_requested ? "停止中" : KNOWLEDGE_JOB_LABELS[job.state] || job.state;
  const parts = [el("strong", { text: job.trigger === "schedule" ? `${label}（自動）` : label })];
  if (job.state === "running") {
    parts.push(knowledgeProgressBar(job.done, job.total));
    parts.push(el("div", { class: "hint", text: `${job.done} / ${job.total || "?"} ファイル` }));
    if (job.current) parts.push(el("div", { class: "hint", text: job.current }));
  }
  if (job.result) {
    const r = job.result;
    parts.push(el("div", { class: "hint", text: `追加 ${r.added} / 更新 ${r.updated} / 変更なし ${r.unchanged} / 削除 ${r.removed} / 失敗 ${r.failed.length}` }));
    if (r.failed.length) {
      const details = el("details", {}, [el("summary", { text: "失敗したファイル" })]);
      for (const f of r.failed) details.append(el("div", { class: "hint", text: `${f.path}: ${f.reason}` }));
      parts.push(details);
    }
    // 上限サイズ超えは設定で対処できるため、失敗一覧を開かなくても見えるように対処方法を出す
    // （src/vector_db/ingest.py の _too_large_hint と同じ内容）。
    if (r.too_large) {
      parts.push(
        el("div", {
          class: "warn-box",
          text:
            `${r.too_large} 件は上限サイズ（[vector_db] max_file_mb = ${maxFileMb}MB。Locohane 側の制限）を超えたため取り込んでいません。` +
            "config.ini タブ（「ベクターDB」プリセット）で max_file_mb を大きくしてから「取り込み（差分）」を実行し直すと取り込めます。",
        })
      );
    }
  }
  // ファイル数の上限超えも設定で対処できるため、上限の変え方まで出す（src/vector_db/ingest.py の _over_limit_hint と同じ内容）。
  if (job.result && job.result.over_limit) {
    const r = job.result;
    parts.push(
      el("div", {
        class: "warn-box",
        text:
          `取り込み元に ${r.found_files} 件のファイルがあり、1コレクションの上限（config.ini [vector_db] max_files_per_collection = ${r.max_files}）を` +
          `超えたため、${r.over_limit} 件は取り込んでいません（取り込み済みのファイルを優先し、残りはパス順の先頭から選んでいます）。` +
          "config.ini タブ（「ベクターDB」プリセット）で max_files_per_collection を大きくしてから「取り込み（差分）」を実行し直すと取り込めます" +
          "（取り込み中は対象のファイルの一覧をメモリーに持つため、管理ツールの PC のメモリーに注意してください）。" +
          "コレクションを取り込み元のフォルダごとに分けることもできます。",
      })
    );
  }
  if (job.error) parts.push(el("div", { class: "error", text: job.error }));
  if (job.finished_at) parts.push(el("div", { class: "hint", text: `終了 ${fmtTime(job.finished_at)}` }));
  return el("div", {}, parts);
}

// 取り込みの進捗バー。対象ファイルを数え終わるまで（total が 0）は割合が出せないため、流れる表示にする。
function knowledgeProgressBar(done, total) {
  if (!total) return el("div", { class: "kn-progress indeterminate" }, [el("div", { class: "kn-progress-fill" })]);
  const pct = Math.min(100, Math.floor((done / total) * 100));
  return el("div", { class: "kn-progress", title: `${pct}%` }, [
    el("div", { class: "kn-progress-fill", style: `width: ${pct}%` }),
    el("span", { class: "kn-progress-text", text: `${pct}%` }),
  ]);
}

function knowledgeActions(container, name, c, enabled) {
  const running = c.job && c.job.state === "running";
  const path = `/collections/${encodeURIComponent(c.name)}`;
  const buttons = [];
  if (running) {
    // 停止はすぐには効かない（処理中のファイルの埋め込みを待つ）ため、押したらその場で「停止中…」にして押せなくする。
    const stopBtn = el("button", {
      text: c.job.stop_requested ? "停止中…" : "停止",
      onclick: async () => {
        stopBtn.disabled = true;
        stopBtn.textContent = "停止中…";
        if (await knowledgeAction(() => api(knowledgePath(name, `${path}/stop`), { method: "POST" }))) renderKnowledgePanel(container, name);
        else {
          stopBtn.disabled = false;
          stopBtn.textContent = "停止";
        }
      },
    });
    stopBtn.disabled = !!c.job.stop_requested;
    buttons.push(stopBtn);
  } else {
    const ingest = (rebuild) => async () => {
      if (rebuild && !confirm(`${c.name} のベクターをすべて消して、全ファイルを取り込み直します。よろしいですか？（埋め込みモデルを変えたときに必要です）`)) return;
      if (await knowledgeAction(() => api(knowledgePath(name, `${path}/ingest`), { method: "POST", body: { rebuild } }))) renderKnowledgePanel(container, name);
    };
    const ingestBtn = el("button", { class: "primary", text: "取り込み（差分）", onclick: ingest(false) });
    const rebuildBtn = el("button", { text: "再構築", onclick: ingest(true) });
    ingestBtn.disabled = !enabled;
    rebuildBtn.disabled = !enabled;
    buttons.push(ingestBtn, rebuildBtn);
    buttons.push(el("button", { text: "編集", onclick: () => openEditCollectionForm(container, name, c) }));
    buttons.push(
      el("button", {
        class: "btn-danger",
        text: "削除",
        onclick: async () => {
          if (!confirm(`コレクション ${c.name} と取り込み済みのベクターを削除します（取り込み元の文書は消えません）。よろしいですか？`)) return;
          if (await knowledgeAction(() => api(knowledgePath(name, path), { method: "DELETE" }))) renderKnowledgePanel(container, name);
        },
      })
    );
  }
  return el("div", { class: "inline-actions" }, buttons);
}

function collectionFields(c) {
  const weekdays = KNOWLEDGE_WEEKDAYS.map((label, i) => {
    const box = el("input", { type: "checkbox" });
    box.checked = !!c && c.schedule_weekdays.includes(i);
    return { index: i, label, box };
  });
  const scheduleEnabled = el("input", { type: "checkbox" });
  scheduleEnabled.checked = !!c && c.schedule_enabled;
  return {
    description: el("input", { type: "text", value: c ? c.description : "", placeholder: "例: 品質管理規程・手順書" }),
    folder: el("input", { type: "text", value: c ? c.source_folder : "", placeholder: "例: D:\\shared\\manuals（管理ツールの PC から見える絶対パス）" }),
    recursive: el("input", { type: "checkbox" }),
    scheduleEnabled,
    scheduleTimes: el("input", { type: "text", value: c ? c.schedule_times.join(", ") : "", placeholder: "例: 02:00（複数はカンマ区切り。例: 07:30, 19:00）" }),
    weekdays,
  };
}

// 自動取り込みの入力欄（追加フォームと編集ダイアログで共通）。
function scheduleFieldElements(f) {
  const toggle = () => {
    f.scheduleTimes.disabled = !f.scheduleEnabled.checked;
    for (const w of f.weekdays) w.box.disabled = !f.scheduleEnabled.checked;
  };
  f.scheduleEnabled.addEventListener("change", toggle);
  toggle();
  return [
    el("label", { class: "checkbox" }, [f.scheduleEnabled, " 時間指定で自動取り込み（差分）を行う"]),
    el("label", {}, ["時刻（24時間表記）", f.scheduleTimes]),
    el("div", {}, [
      el("div", { text: "曜日（どれも選ばなければ毎日）" }),
      el(
        "div",
        { class: "inline-actions" },
        f.weekdays.map((w) => el("label", { class: "checkbox" }, [w.box, ` ${w.label}`]))
      ),
    ]),
    el("p", { class: "hint", text: "管理ツール（admin.bat）が起動している間だけ実行します。止まっていた間の時刻の分は後から実行しません。" }),
  ];
}

function scheduleBody(f) {
  return {
    schedule_enabled: f.scheduleEnabled.checked,
    schedule_times: f.scheduleTimes.value.split(/[,、\s]+/).filter((t) => t),
    schedule_weekdays: f.weekdays.filter((w) => w.box.checked).map((w) => w.index),
  };
}

function buildCreateCollectionForm(container, name) {
  const nameInput = el("input", { type: "text", placeholder: "例: manuals（半角英数字・_・- の48文字以内）", maxlength: "48" });
  const f = collectionFields(null);
  f.recursive.checked = true;
  const error = el("div", { class: "error" });
  const form = el("form", { class: "knowledge-form" }, [
    el("h3", { text: "コレクションを追加" }),
    el("label", {}, ["コレクション名", nameInput]),
    el("label", {}, ["説明（LLM にも一覧で見せる）", f.description]),
    el("label", {}, ["取り込み元フォルダ", f.folder]),
    el("label", { class: "checkbox" }, [f.recursive, " サブフォルダも取り込む"]),
    ...scheduleFieldElements(f),
    error,
    el("button", { type: "submit", class: "primary", text: "追加" }),
  ]);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.textContent = "";
    try {
      await api(knowledgePath(name, "/collections"), {
        method: "POST",
        body: {
          name: nameInput.value.trim(),
          description: f.description.value,
          source_folder: f.folder.value,
          recursive: f.recursive.checked,
          ...scheduleBody(f),
        },
      });
      renderKnowledgePanel(container, name);
    } catch (e) {
      error.textContent = e.message;
    }
  });
  return form;
}

function openEditCollectionForm(container, name, c) {
  const f = collectionFields(c);
  f.recursive.checked = c.recursive;
  const error = el("div", { class: "error" });
  const backdrop = el("div", { class: "modal-backdrop" });
  const close = () => backdrop.remove();
  const form = el("form", {}, [
    el("h3", { text: `コレクション ${c.name} を編集` }),
    el("label", {}, ["説明", f.description]),
    el("label", {}, ["取り込み元フォルダ", f.folder]),
    el("label", { class: "checkbox" }, [f.recursive, " サブフォルダも取り込む"]),
    el("p", { class: "hint", text: "フォルダを変えた場合、次の取り込みで新しいフォルダに無いファイルのベクターは削除されます。" }),
    ...scheduleFieldElements(f),
    error,
    el("div", { class: "modal-actions" }, [el("button", { type: "button", text: "キャンセル", onclick: close }), el("button", { type: "submit", class: "primary", text: "保存" })]),
  ]);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.textContent = "";
    try {
      await api(knowledgePath(name, `/collections/${encodeURIComponent(c.name)}`), {
        method: "PUT",
        body: { description: f.description.value, source_folder: f.folder.value, recursive: f.recursive.checked, ...scheduleBody(f) },
      });
      close();
      renderKnowledgePanel(container, name);
    } catch (e) {
      error.textContent = e.message;
    }
  });
  backdrop.append(el("div", { class: "modal" }, form));
  document.body.append(backdrop);
}
