// dsh-resume Node 桥：把插件的「指令生成 / 反编造校验 / docx 导出」暴露给 Python 侧。
//
// 用法：echo '<json>' | node resume_bridge.mjs
//   {action:"ping", plugin_dir}                     -> {ok, guard, prompt}
//   {action:"instruction", plugin_dir, jd, resume, language}
//   {action:"verify", plugin_dir, original, rewritten, claimedKeywords}
//   {action:"export", plugin_dir, resume, changes, gaps, verification} -> {markdown, base64}
//
// 复用插件自身的生产代码（不是复刻），保证校验口径与 DSH 里完全一致。
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

function emit(obj) {
  process.stdout.write(JSON.stringify(obj));
}

let input;
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch (error) {
  emit({ ok: false, error: `桥接输入不是合法 JSON: ${error.message}` });
  process.exit(0);
}

const pluginDir = input.plugin_dir;
if (!pluginDir) {
  emit({ ok: false, error: "缺少 plugin_dir" });
  process.exit(0);
}

let plugin;
try {
  plugin = await import(pathToFileURL(join(pluginDir, "src", "index.js")).href);
} catch (error) {
  emit({ ok: false, error: `无法加载插件 ${pluginDir}: ${error.message}` });
  process.exit(0);
}

try {
  switch (input.action) {
    case "ping":
      emit({
        ok: true,
        guard: plugin.GUARD_VERSION ?? null,
        hasInstruction: typeof plugin.buildResumeValue === "function",
        hasVerify: typeof plugin.verifyRewrite === "function",
        hasDocx: typeof plugin.buildDocx === "function",
      });
      break;

    case "instruction": {
      const value = plugin.buildResumeValue({
        jd: input.jd,
        resume: input.resume,
        language: input.language || "zh",
      });
      emit({ ok: true, ...value });
      break;
    }

    case "verify": {
      const report = plugin.verifyRewrite({
        original: input.original,
        rewritten: input.rewritten,
        claimedKeywords: input.claimedKeywords || [],
      });
      emit({ ok: true, report });
      break;
    }

    case "export": {
      const markdown = plugin.assembleDeliverable({
        resume: input.resume,
        changes: input.changes,
        gaps: input.gaps,
        verification: input.verification,
      });
      const buf = plugin.buildDocx(markdown);
      emit({
        ok: true,
        markdown,
        base64: Buffer.from(buf).toString("base64"),
        bytes: buf.length,
      });
      break;
    }

    default:
      emit({ ok: false, error: `未知 action: ${input.action}` });
  }
} catch (error) {
  emit({ ok: false, error: String((error && error.message) || error) });
}
