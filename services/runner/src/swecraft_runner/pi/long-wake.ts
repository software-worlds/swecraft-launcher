/**
 * long-wake — SWECraft's long-wake contract for pi.
 *
 * pi in --print mode answers one prompt and exits as soon as the model
 * replies without a tool call: a single-task wake. A long wake keeps the
 * agent working until its budget is spent. The coordinator states the mode
 * and the budget in the agent env (swecraft_coordinator: wake_mode, wake_budget);
 * the agent class adds the policy below (swecraft_runner.pi.LongWakePolicy).
 *
 *   SWECRAFT_WAKE_MODE          "long-wake" enables all of this; anything else
 *                          leaves pi's one-shot run alone
 *   SWECRAFT_WAKE_TURNS, SWECRAFT_WAKE_SECONDS, SWECRAFT_WAKE_USD
 *                          the budget; 0 = unlimited; the first kind exceeded
 *                          counts. Cost is pi's own per-message figure, the
 *                          number the coordinator books
 *   SWECRAFT_WAKE_DEADLINE_SECONDS
 *                          the coordinator's deadline, just under the
 *                          runner's timeout (a hard kill): treated as a time
 *                          budget of its own, so the finalize and the hard
 *                          stop below happen before the kill whatever budget
 *                          kinds are configured
 *   SWECRAFT_WAKE_FINALIZE_AT   fraction of the budget at which the finalize
 *                          notice is steered in (repeated every ten turns)
 *   SWECRAFT_WAKE_HARD_STOP_AT  fraction at which the harness takes over: every
 *                          tool but reading, writing the result, the
 *                          response and the notes, and the memory CLI is
 *                          blocked, the model is told to write its final
 *                          files, and after SWECRAFT_FINAL_TURNS turns without
 *                          them the harness writes the envelope itself and
 *                          exits
 *   SWECRAFT_IDLE_CONTINUATIONS consecutive runs without a tool call before
 *                          finalizing
 *   SWECRAFT_ERROR_RUNS, SWECRAFT_ERROR_DELAY_S
 *                          a run ended by a provider error pi retries itself
 *                          is left to pi (its retry settings and classifier
 *                          are mirrored here, since extensions see neither
 *                          the retry events nor the decision); one pi gives
 *                          up on — retries exhausted, or an error it does not
 *                          retry — is resumed after the delay (never counted
 *                          as idle), and past the cap of such runs in a row
 *                          the wake ends as blocked. A context overflow pi
 *                          cannot compact ends the wake at once. A plan or
 *                          quota limit ("usage limit", "quota exceeded";
 *                          waited out first when SWECRAFT_LIMIT_WAIT_S says so)
 *                          or a login the provider no longer accepts (never
 *                          waited out: only a new login helps) refuses
 *                          every call until an operator acts, so it ends
 *                          the wake at once too: as blocked, with the
 *                          envelope, when the wake had billed work whose
 *                          notes should publish; without the final files
 *                          when nothing was billed, so the runner books the
 *                          wake failed (a missing response file) and the
 *                          coordinator backs off instead of dispatching
 *                          another wake every interval
 *   SWECRAFT_CONTEXT_HARD_TOKENS
 *                          measured on the context the provider reports: at
 *                          hard, or after pi's compaction failed twice in a
 *                          row, the wake is finalized (0 disables). The soft
 *                          budget is where the agent class sets pi's own
 *                          compaction to fire (through its reserve), so it
 *                          needs nothing from this extension
 *   SWECRAFT_PROGRESS_EVERY     a budget-used line every N turns (0 disables)
 *   SWECRAFT_HANG_MINUTES       a model call with no response for this long is a
 *                          call that will never return; twice a Codex
 *                          call never returned and the process sat
 *                          idle until the runner's timeout (0 disables)
 *   SWECRAFT_HANG_ABORTS        how many such calls are aborted and the run
 *                          resumed (the session keeps its context) before a
 *                          hang ends the wake as blocked, with the envelope,
 *                          so the coordinator books it (0: the first hang
 *                          ends the wake)
 *   SWECRAFT_FORCE_CONTINUES    budget forcing: an envelope written before the
 *                          finalize notice does not end the wake. It is set
 *                          aside, the model is told how much of the budget
 *                          is unused and sent back to work, this many times
 *                          at most; the last envelope written wins (0: the
 *                          first envelope ends the wake, as before). One
 *                          long session of ours ended itself at $14 of
 *                          $103
 *   SWECRAFT_LIMIT_WAIT_S       a plan or quota refusal is waited out for up to
 *                          this long in all (polled every five minutes,
 *                          each poll one refused, unbilled call) before it
 *                          ends the wake as below; the wake's own budget and
 *                          deadline keep running meanwhile. For sessions
 *                          meant to last days, where a forty-minute usage
 *                          burst must not cost the session (0: refuse at
 *                          once, right for ten-minute wakes since the
 *                          coordinator then backs off)
 *   (no knob)              a bash command whose first word is a tool name
 *                          fused with letters, or that carries an undecodable
 *                          character, is refused as corrupted tool text and
 *                          the model asked to restate it
 *   SWECRAFT_RESULT_PATH        the envelope; once it holds a valid envelope the
 *                          wake ends at the model's next reply without a
 *                          tool call. A file that is not the envelope (the
 *                          first long wakes wrote status reports of their
 *                          own there, mostly after a compaction) is sent
 *                          back twice with the schema before the wake is
 *                          let go with whatever it holds
 *
 * The agent ends the wake itself: after the finalize notice it writes the
 * envelope and response.md and replies without calling a tool, which lets
 * pi exit. Every harness-ended path writes the envelope so the coordinator
 * can book the wake, and the pi process is never signalled from outside —
 * Harbor books a killed agent as a failed trial whatever it left behind.
 */
import { existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { isContextOverflow, isRetryableAssistantError } from "@earendil-works/pi-ai";
import type { AssistantMessage } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

// Provider messages no retry can get past. A plan or quota limit (pi's own
// non-retryable limit patterns plus the Codex phrasing) clears with time —
// the weekly window resets, a usage burst passes — so it may be waited out.
// A login the provider no longer accepts (a fresh login elsewhere
// invalidates the Codex token a run was started with) clears only with a
// new login, which a running wake cannot get.
const QUOTA_ERROR =
	/usage.?limit|quota|insufficient_quota|out of budget|available balance|billing|GoUsageLimitError|FreeUsageLimitError|try again in ~?\d+ ?min/i;
const LOGIN_ERROR = /invalidated oauth token|invalid.?token|token.?expired|unauthori[sz]ed|authentication.?(failed|error)|\b401\b/i;
const LIMIT_POLL_SECONDS = 300;
// A bash command whose first word is a known tool name fused with letters
// (`sleeparthy 600`, `forgeWing?`), or that carries an undecodable character,
// is corrupted tool text, not a command: long sessions of ours produced 64
// such calls in 10,822, clustering right after aborted provider streams.
// Refusing it costs the model one restated call; running it costs a wasted
// turn at best.
const CORRUPT_COMMAND = /(^|[\s;&|(])(sleep|forge|fj|git|swecraft-memory)[A-Za-z]{2,}\b|\uFFFD/;

const num = (name: string, fallback: number): number => {
	const raw = process.env[name];
	const parsed = raw === undefined || raw === "" ? Number.NaN : Number(raw);
	return Number.isFinite(parsed) ? parsed : fallback;
};

// pi's own retry and compaction settings, from the file the agent class
// wrote (swecraft_runner.pi.pi_settings), with pi's defaults behind them: the
// provider-error handling below mirrors what pi itself will do next.
const piSettings = (): { retryEnabled: boolean; maxRetries: number; compaction: boolean } => {
	try {
		const raw = JSON.parse(readFileSync(join(homedir(), ".pi", "agent", "settings.json"), "utf8")) as {
			retry?: { enabled?: boolean; maxRetries?: number };
			compaction?: { enabled?: boolean };
		};
		return {
			retryEnabled: raw.retry?.enabled ?? true,
			maxRetries: raw.retry?.maxRetries ?? 3,
			compaction: raw.compaction?.enabled ?? true,
		};
	} catch {
		return { retryEnabled: true, maxRetries: 3, compaction: true };
	}
};

export default function (pi: ExtensionAPI) {
	const long = process.env.SWECRAFT_WAKE_MODE === "long-wake";
	const budget = {
		turns: num("SWECRAFT_WAKE_TURNS", 0),
		seconds: num("SWECRAFT_WAKE_SECONDS", 0),
		usd: num("SWECRAFT_WAKE_USD", 0),
	};
	const finalizeAt = num("SWECRAFT_WAKE_FINALIZE_AT", 0.97);
	const hardStopAt = num("SWECRAFT_WAKE_HARD_STOP_AT", 1.05);
	// The deadline is where the hard stop must land, so as a budget it is the
	// deadline divided by the hard-stop fraction.
	const deadlineSeconds = num("SWECRAFT_WAKE_DEADLINE_SECONDS", 0);
	const deadlineBudget = deadlineSeconds > 0 ? deadlineSeconds / hardStopAt : 0;
	const idleCap = num("SWECRAFT_IDLE_CONTINUATIONS", 20);
	const errorCap = num("SWECRAFT_ERROR_RUNS", 60);
	const errorDelaySeconds = num("SWECRAFT_ERROR_DELAY_S", 60);
	const progressEvery = num("SWECRAFT_PROGRESS_EVERY", 100);
	const hangMinutes = num("SWECRAFT_HANG_MINUTES", 20);
	const hangAborts = num("SWECRAFT_HANG_ABORTS", 0);
	const forceContinues = num("SWECRAFT_FORCE_CONTINUES", 0);
	const limitWaitSeconds = num("SWECRAFT_LIMIT_WAIT_S", 0);
	const hardTokens = num("SWECRAFT_CONTEXT_HARD_TOKENS", 300_000);
	const finalTurns = num("SWECRAFT_FINAL_TURNS", 10);
	const resultPath = process.env.SWECRAFT_RESULT_PATH ?? "/app/result.json";
	const responsePath = resultPath.replace(/result\.json$/, "response.md");
	// Notes live in /app/memory when the coordinator mounted it; a wake that
	// resumes its conversation instead has none, and the notices say so.
	const memoryDir = "/app/memory";
	const hasMemory = existsSync(memoryDir);

	const startedAt = Date.now();
	let turns = 0;
	let usd = 0;
	let finalizeSent = false;
	let hardStopped = false;
	let finalTurnsLeft = 0;
	let idleRuns = 0;
	let errorRuns = 0;
	let compactionFailures = 0;
	let lastRunHadToolCalls = false;
	let lastRunErrored = false;
	let lastAssistant: AssistantMessage | null = null;
	// pi's retry counter for the current streak of failed calls, kept in step
	// with pi: up on every retry pi starts, back to zero once a call succeeds.
	let retryAttempt = 0;
	let overflowPending = false;
	let envelopeCorrections = 0;
	let callStartedAt: number | null = null; // an assistant call in flight since
	let lastContext: ExtensionContext | null = null; // for aborting a hung call from the timer
	let hangAbortsUsed = 0;
	let abortedAt: number | null = null; // a hung call aborted, its run not yet ended
	let forced = 0;
	let earlyEnvelope: { outcome: string; summary: string } | null = null;
	let limitWaitedSeconds = 0;
	let deliberateFailure = false;
	const settings = piSettings();

	const OUTCOMES = new Set(["worked", "idle", "blocked"]);
	// The wake envelope: {"outcome": worked|idle|blocked, "summary": "<one line>"}.
	const envelopeValid = (): boolean => {
		try {
			const parsed = JSON.parse(readFileSync(resultPath, "utf8")) as { outcome?: unknown; summary?: unknown };
			return (
				typeof parsed === "object" &&
				parsed !== null &&
				typeof parsed.outcome === "string" &&
				OUTCOMES.has(parsed.outcome) &&
				typeof parsed.summary === "string" &&
				parsed.summary.trim().length > 0
			);
		} catch {
			return false;
		}
	};

	const log = (message: string) => console.error(`[swecraft-wake] ${message}`);

	const fraction = (): number => {
		const seconds = (Date.now() - startedAt) / 1000;
		let used = 0;
		if (budget.turns > 0) used = Math.max(used, turns / budget.turns);
		if (budget.seconds > 0) used = Math.max(used, seconds / budget.seconds);
		if (budget.usd > 0) used = Math.max(used, usd / budget.usd);
		if (deadlineBudget > 0) used = Math.max(used, seconds / deadlineBudget);
		return used;
	};
	const describe = (): string => {
		const parts: string[] = [];
		if (budget.turns > 0) parts.push(`turn ${turns} of ${budget.turns}`);
		if (budget.seconds > 0) {
			const hours = (Date.now() - startedAt) / 3_600_000;
			parts.push(`${hours.toFixed(1)} h of ${(budget.seconds / 3600).toFixed(1)} h`);
		}
		if (budget.usd > 0) parts.push(`$${usd.toFixed(2)} of $${budget.usd.toFixed(2)}`);
		if (budget.seconds <= 0 && deadlineSeconds > 0) {
			const hours = (Date.now() - startedAt) / 3_600_000;
			parts.push(`${hours.toFixed(1)} h elapsed of a ${(deadlineSeconds / 3600).toFixed(1)} h deadline`);
		}
		return parts.length > 0 ? parts.join(", ") : `turn ${turns}`;
	};

	// The harness envelope fills a gap only: a valid envelope the model wrote
	// is never overwritten. An envelope the model wrote early and was sent
	// back from still describes the work: the harness's reason is appended.
	const writeResult = (outcome: string, summary: string) => {
		try {
			if (!envelopeValid()) {
				const final = earlyEnvelope
					? { outcome: earlyEnvelope.outcome, summary: `${earlyEnvelope.summary} (the session continued past this envelope; ${summary})` }
					: { outcome, summary };
				writeFileSync(resultPath, `${JSON.stringify(final)}\n`);
			}
			if (!existsSync(responsePath)) {
				writeFileSync(responsePath, `# Wake ended by the harness\n\n${summary}\n`);
			}
		} catch (error) {
			log(`could not write ${resultPath}: ${String(error)}`);
		}
	};

	// Ends the process on a harness decision, only ever after the envelope is
	// written and from a quiet point (the session JSONL is append-only and
	// already flushed).
	const endWake = (summary: string) => {
		log(summary);
		writeResult("blocked", summary);
		setTimeout(() => process.exit(0), 50);
	};
	// Ends the process without the final files: the runner books a wake
	// whose response file is missing as failed, and the coordinator backs
	// off. (A response file without an envelope books as completed with no
	// outcome, and the coordinator dispatches again an interval later — the
	// first version wrote one, and a revoked login produced three hundred
	// zero-cost rows in two hours.) Only for a wake in which nothing was
	// billed, so no notes are lost; the reason goes to the agent log.
	const endWakeFailed = (summary: string) => {
		deliberateFailure = true;
		log(summary);
		try {
			writeFileSync("/logs/agent/swecraft-wake-failed.txt", `${summary}\n`);
		} catch (error) {
			log(`could not write the failure note: ${String(error)}`);
		}
		setTimeout(() => process.exit(0), 50);
	};

	// "steer" is sent from turn_end, while the run is streaming: pi queues it
	// for the model's next turn of this run. It must not carry
	// triggerTurn:false — with that, a streaming session defers the message
	// to its state only, so the running loop never sends it to the model
	// (nothing shows it until the run restarts). "followUp" is sent from
	// agent_end, when the run is over, and
	// starts the next run with the message as its prompt.
	const notice = (content: string, delivery: "steer" | "followUp") => {
		pi.sendMessage(
			{ customType: "swecraft-wake", content, display: true },
			delivery === "steer" ? { deliverAs: "steer" } : { deliverAs: "followUp", triggerTurn: true },
		);
	};

	const finalFiles = `${resultPath}, ${responsePath}${hasMemory ? ` and files under ${memoryDir}` : ""}`;
	// The envelope schema is restated here because these notices arrive hours
	// after the instructions that first gave it, possibly after compaction.
	const envelope = `${resultPath} as exactly {"outcome": "worked" | "idle" | "blocked", "summary": "<one line>"}`;
	const handoff = hasMemory
		? `update ${memoryDir}/notebook.md and the daily log in place (they are committed for you; never run git inside ${memoryDir}), then write ${envelope} and ${responsePath} (your report for this wake)`
		: `write ${envelope} and ${responsePath} (your report for this wake)`;
	const afterwards = hasMemory ? "a new wake will start from your notes" : "the next wake resumes this conversation";

	const finalizeNotice = (reason: string) =>
		`[SWECRAFT WAKE] ${reason} This wake ends soon: stop starting new work, finish or safely park what is in flight, ${handoff}, then reply without calling any tool. That final reply ends the wake; ${afterwards}.`;
	const continuation = () =>
		`[SWECRAFT WAKE] ${describe()}. The wake continues: re-check state (notifications, your repository${hasMemory ? ", your notes" : ""}), choose the next objective${hasMemory ? " in notebook.md" : ""}, and act. If nothing needs you right now, wait with \`timeout 700 sleep 600\` and check again — never longer than that in one call, since a sleeping call cannot receive the notices this wake depends on. Do not end the wake on your own before the finalize notice.`;
	const hardStopNotice = () =>
		`[SWECRAFT WAKE] The wake's budget is exhausted (${describe()}). Tools are now limited to writing ${finalFiles}${hasMemory ? " and `swecraft-memory` for the daily log" : ""}. First write ${envelope} and ${responsePath}${hasMemory ? ", then update your notes with whatever turns remain" : ""}; then reply without calling any tool. That final reply ends the wake.`;

	// A finalize decision made mid-run (the model is still calling tools) is
	// steered into its next turn; one made at agent_end, when the model is
	// idle, must start a turn itself.
	const finalize = (reason: string, delivery: "steer" | "followUp") => {
		finalizeSent = true;
		notice(finalizeNotice(reason), delivery);
	};
	const hardStop = (delivery: "steer" | "followUp") => {
		hardStopped = true;
		finalTurnsLeft = finalTurns;
		log(`hard stop: ${describe()}`);
		notice(hardStopNotice(), delivery);
	};

	// A run that ends on a provider error is not an idle run: the model never
	// got to answer. Cost accrues per message. A call that succeeds ends pi's
	// retry streak, as it does in pi.
	pi.on("message_start", (event, ctx: ExtensionContext) => {
		const message = event.message as { role?: string };
		if (message.role === "assistant") {
			callStartedAt = Date.now();
			lastContext = ctx;
		}
	});

	pi.on("message_end", (event) => {
		const message = event.message as {
			role?: string;
			stopReason?: string;
			usage?: { cost?: { total?: number } };
		};
		if (message.role === "assistant") callStartedAt = null;
		if (message.role !== "assistant") return;
		lastRunErrored = message.stopReason === "error";
		lastAssistant = event.message as AssistantMessage;
		if (!lastRunErrored) retryAttempt = 0;
		usd += message.usage?.cost?.total ?? 0;
	});

	// Hang guard: a model call that has produced no response for hangMinutes
	// is a call that will never return (pi's own idle timeout covers a stream
	// that started, not one that did not). Up to hangAborts times the call is
	// aborted and the run resumed from agent_end with the session intact —
	// a session meant to last days should not end on one dead connection.
	// Past that, or if the aborted run does not end within two minutes, the
	// wake ends as blocked with the envelope, from inside pi, so the ledger
	// books it and the next wake can start; the work already pushed stands.
	if (long && hangMinutes > 0) {
		const timer = setInterval(() => {
			if (abortedAt !== null && Date.now() - abortedAt > 120_000) {
				clearInterval(timer);
				endWake(`Aborted a hung provider call but the run did not end (${describe()}); wake ended by the harness.`);
				return;
			}
			if (callStartedAt === null || existsSync(resultPath)) return;
			const minutes = (Date.now() - callStartedAt) / 60_000;
			if (minutes < hangMinutes) return;
			if (hangAbortsUsed < hangAborts && lastContext !== null && abortedAt === null) {
				hangAbortsUsed += 1;
				abortedAt = Date.now();
				log(`provider call hung for ${Math.round(minutes)} min; aborting it and resuming (${hangAbortsUsed}/${hangAborts})`);
				lastContext.abort();
				return;
			}
			clearInterval(timer);
			endWake(`Provider call hung for ${Math.round(minutes)} min (${describe()}); wake ended by the harness.`);
		}, 30_000);
		timer.unref?.();
	}

	// pi exiting without the final files (a resume that did not start after
	// an aborted call, an exit the harness did not decide) would book the
	// wake failed and lose its notes; the envelope is written on the way out
	// unless the exit without files was the harness's own decision.
	if (long) {
		process.on("exit", () => {
			if (!deliberateFailure && !existsSync(resultPath)) {
				writeResult("blocked", `pi exited before the final files were written (${describe()}); envelope written by the harness.`);
			}
		});
	}

	// After the hard stop only the wrap-up the prompt describes may run:
	// reading, writing the final files and the notes, and the memory CLI
	// the daily log and lessons go through (`swecraft-memory`, a bash call).
	// Every other tool call is refused with the reason, and still counts as
	// activity. (The first live long wakes ended blocked on exactly this:
	// the model was recording its daily line when the harness refused it.)
	pi.on("tool_call", (event) => {
		lastRunHadToolCalls = true;
		if (!long) return;
		if (event.toolName === "bash") {
			const command = (event.input as { command?: unknown }).command;
			if (typeof command === "string" && CORRUPT_COMMAND.test(command)) {
				log(`refused a bash command that looks corrupted: ${command.slice(0, 80)}`);
				return {
					block: true,
					reason: "The command text looks corrupted (a tool name fused with other characters, or an undecodable character). Restate the command exactly as you intend it.",
				};
			}
		}
		if (!hardStopped) return;
		const input = event.input as { path?: unknown; command?: unknown };
		if (event.toolName === "read") return;
		if (
			(event.toolName === "write" || event.toolName === "edit") &&
			typeof input.path === "string" &&
			(input.path === resultPath || input.path === responsePath || (hasMemory && input.path.startsWith(`${memoryDir}/`)))
		) {
			return;
		}
		if (event.toolName === "bash" && typeof input.command === "string" && /(^|[\s;&|(])swecraft-memory(\s|$)/.test(input.command)) {
			return;
		}
		return {
			block: true,
			reason: `The wake's budget is exhausted: only writing ${finalFiles}${hasMemory ? " and recording notes with `swecraft-memory`" : ""} is possible now. Write them, then reply without calling any tool.`,
		};
	});

	// pi's compaction is the within-wake mechanism. When it fails twice in a
	// row the transcript can no longer be condensed by anything, and every
	// further turn re-sends all of it: end cleanly while the model can still
	// write its notes.
	pi.on("compaction_end", (event) => {
		if (!long) return;
		if (event.errorMessage) {
			compactionFailures += 1;
			log(`compaction failed (${compactionFailures} in a row): ${event.errorMessage}`);
			if (overflowPending) {
				// pi was compacting to recover from an overflow and could not:
				// nothing continues the run after this, so end it with the envelope.
				endWake(`Context overflow and pi's compaction failed (${describe()}); wake ended by the harness.`);
				return;
			}
			if (compactionFailures >= 2 && !finalizeSent) {
				finalize("Context compaction failed twice; the transcript can no longer be condensed.", "steer");
			}
			return;
		}
		compactionFailures = 0;
		overflowPending = false;
	});

	pi.on("turn_end", (_event, ctx: ExtensionContext) => {
		turns += 1;
		if (!long) return;
		if (hardStopped) {
			if (envelopeValid()) return; // written: the wake ends at the next reply without a tool call
			finalTurnsLeft -= 1;
			if (finalTurnsLeft <= 0) {
				endWake(`Budget exhausted (${describe()}); the agent did not write its final files within ${finalTurns} turns.`);
			}
			return;
		}
		if (fraction() >= hardStopAt) {
			hardStop("steer");
			return;
		}
		// The usage of the call that started this turn: a compaction written
		// during the turn shows only in the next call's count.
		const tokens = ctx.getContextUsage?.()?.tokens ?? null;
		if (tokens !== null && hardTokens > 0 && tokens >= hardTokens && !finalizeSent) {
			// Being here means pi's compaction did not bring the context under the
			// limit: finalize while the model can still write its notes.
			finalize(`Context is ${tokens} tokens, past the wake's ${hardTokens} limit.`, "steer");
			return;
		}
		if (!finalizeSent && fraction() >= finalizeAt) {
			finalize(`Budget nearly spent (${describe()}).`, "steer");
			return;
		}
		if (finalizeSent) {
			// Repeat the notice every ten turns until the envelope appears.
			if (turns % 10 === 0) {
				notice(finalizeNotice(`Reminder: ${describe()}; the harness takes over at ${Math.round(hardStopAt * 100)}% of the budget.`), "steer");
			}
			return;
		}
		// A busy model never sees the continuation messages; a progress line
		// keeps the budget from being a surprise so in-flight work is sized to fit.
		if (progressEvery > 0 && turns % progressEvery === 0) {
			notice(
				`[SWECRAFT WAKE] ${describe()}. Keep in-flight work small enough to finish before the budget is spent${hasMemory ? ", and keep notebook.md current" : ""}.`,
				"steer",
			);
		}
	});

	pi.on("agent_end", async (_event, ctx: ExtensionContext) => {
		if (!long) return;
		abortedAt = null; // the run ended, aborted or not
		if (existsSync(resultPath)) {
			// Budget forcing: an envelope written before the finalize notice,
			// while most of the budget is unused, is set aside and the model
			// sent back to work, a bounded number of times.
			if (envelopeValid() && forced < forceContinues && !finalizeSent && !hardStopped && fraction() < finalizeAt) {
				forced += 1;
				const early = JSON.parse(readFileSync(resultPath, "utf8")) as { outcome: string; summary: string };
				earlyEnvelope = early;
				try {
					renameSync(resultPath, `${resultPath}.early-${forced}`);
				} catch (error) {
					log(`could not set the early envelope aside: ${String(error)}`);
					return;
				}
				const percent = Math.round(fraction() * 100);
				log(`envelope written early, at ${percent}% of the budget (${early.outcome}); sending the session back (${forced}/${forceContinues})`);
				notice(
					`[SWECRAFT WAKE] You ended this session with only ${percent}% of its budget used (${describe()}). The session does not end on your envelope: it ends at the finalize notice, and the budget is meant to be spent. Your envelope ("${early.outcome}: ${early.summary}") is set aside; the last one you write wins. Continue (${forced} of ${forceContinues} continuations): re-check state (notifications, your repository${hasMemory ? ", your notes" : ""}), take the next most valuable item — a further optimization, a measurement you skipped, a review or an answer owed to another maintainer, a release — and act. When the finalize notice arrives, ${handoff}, then reply without calling any tool.`,
					"followUp",
				);
				return;
			}
			if (envelopeValid()) return; // deliverables written: let pi exit
			if (envelopeCorrections >= 2) {
				log(`${resultPath} is still not the wake envelope after ${envelopeCorrections} corrections; letting the wake end with it`);
				return;
			}
			envelopeCorrections += 1;
			log(`${resultPath} is not the wake envelope (correction ${envelopeCorrections}/2)`);
			notice(
				`[SWECRAFT WAKE] ${resultPath} exists but is not the wake envelope, so the wake cannot be booked. Overwrite it with exactly {"outcome": "worked" | "idle" | "blocked", "summary": "<one line on what this wake did>"} — nothing else in that file — keep ${responsePath} as your report, then reply without calling any tool.`,
				"followUp",
			);
			return;
		}
		if (hardStopped) {
			if (finalTurnsLeft <= 0) {
				endWake(`Budget exhausted (${describe()}); the agent did not write its final files.`);
				return;
			}
			notice(hardStopNotice(), "followUp");
			return;
		}
		if (fraction() >= hardStopAt) {
			hardStop("followUp");
			return;
		}
		if (lastRunErrored) {
			lastRunErrored = false;
			lastRunHadToolCalls = false;
			const failed = lastAssistant;
			const error = failed?.errorMessage ?? "unknown error";
			// pi decides its own retry right after this event, with the same
			// classifier and counter as here; a run it retries needs nothing
			// from the harness, and a message queued now would only pile up.
			if (failed !== null && isContextOverflow(failed, ctx.model?.contextWindow ?? 0)) {
				if (settings.compaction) {
					overflowPending = true;
					log(`context overflow; pi compacts and retries: ${error}`);
					return;
				}
				endWake(`Context overflow with pi's compaction off (${describe()}); wake ended by the harness.`);
				return;
			}
			if (QUOTA_ERROR.test(error) && !LOGIN_ERROR.test(error) && limitWaitedSeconds < limitWaitSeconds && fraction() < finalizeAt) {
				// Wait the refusal out: the handler is awaited, so the pause
				// holds pi open, and the resume queued after it starts the
				// next run. Each poll is one refused call, billed nothing.
				const pause = Math.min(LIMIT_POLL_SECONDS, limitWaitSeconds - limitWaitedSeconds);
				limitWaitedSeconds += pause;
				log(`provider refuses calls on a limit (${error}); waiting ${pause}s before trying again (${limitWaitedSeconds}/${limitWaitSeconds}s waited)`);
				await new Promise((resolve) => setTimeout(resolve, pause * 1000));
				notice(
					`[SWECRAFT WAKE] The provider refused the previous call on a usage limit; the harness waited ${Math.round(limitWaitedSeconds / 60)} min and the session resumes. ${continuation()}`,
					"followUp",
				);
				return;
			}
			if (QUOTA_ERROR.test(error) || LOGIN_ERROR.test(error)) {
				if (usd > 0) {
					endWake(`Provider refuses every call (${error}); the wake's work stands and its notes publish; wake ended by the harness.`);
				} else {
					endWakeFailed(`Provider refuses every call before any billed one (${error}); no envelope, so the coordinator backs off.`);
				}
				return;
			}
			if (settings.retryEnabled && failed !== null && isRetryableAssistantError(failed) && retryAttempt < settings.maxRetries) {
				retryAttempt += 1;
				log(`provider error, pi retries (attempt ${retryAttempt}/${settings.maxRetries}): ${error}`);
				return;
			}
			retryAttempt = 0;
			errorRuns += 1;
			if (errorRuns >= errorCap) {
				endWake(`Provider errors pi gave up on, ${errorRuns} runs in a row (${describe()}); wake ended by the harness.`);
				return;
			}
			// pi --print exits as soon as the agent is idle with nothing queued,
			// so the resume is queued synchronously here and the pause is the
			// model's first action rather than a timer in this process.
			log(`provider error pi gave up on, run ${errorRuns}/${errorCap} (${error}); resuming after ${errorDelaySeconds}s`);
			notice(
				`[SWECRAFT WAKE] The previous model call failed on a provider error that pi's own retries did not recover from (${errorRuns} such runs in a row). Wait first: run \`sleep ${errorDelaySeconds}\` as your only action this turn, then continue. ${continuation()}`,
				"followUp",
			);
			return;
		}
		errorRuns = 0;
		idleRuns = lastRunHadToolCalls ? 0 : idleRuns + 1;
		lastRunHadToolCalls = false;
		if (!finalizeSent && idleRuns >= idleCap) {
			finalize("Too many consecutive turns without a tool call.", "followUp");
			return;
		}
		if (finalizeSent) {
			// The notice was delivered but the envelope is still missing: nudge a
			// bounded number of times rather than looping forever.
			if (idleRuns >= 3) {
				endWake(`Finalize notice ignored after ${idleRuns} idle runs (${describe()}); wake ended by the harness.`);
				return;
			}
			notice(finalizeNotice("Reminder: the finalize notice was already sent."), "followUp");
			return;
		}
		notice(continuation(), "followUp");
	});
}
