/**
 * bash-timeout — a default and a ceiling for pi's bash tool timeouts.
 *
 * pi's bash tool has no default timeout: a benchmark that deadlocks stalls
 * the whole run until Harbor's own limit. Every bash call the model issues
 * without a timeout gets SWECRAFT_BASH_TIMEOUT seconds (0 disables); the model
 * may pass its own, larger value for a long benchmark — up to
 * SWECRAFT_BASH_TIMEOUT_MAX seconds when that is set (0: no ceiling). The
 * ceiling exists because a session waiting for work sleeps in a bash call,
 * and two long sessions of ours asked for `sleep 21600` and
 * `sleep 43200` with matching timeouts: hours in which no harness notice
 * can reach the model and the runner's timeout can kill the session
 * mid-tool. A clamped call times out, the model sees that, and polls again.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { isToolCallEventType } from "@earendil-works/pi-coding-agent";

const seconds = (name: string, fallback: number): number => {
	const raw = Number(process.env[name] ?? String(fallback));
	return Number.isFinite(raw) && raw >= 0 ? raw : fallback;
};

export default function (pi: ExtensionAPI) {
	const timeout = seconds("SWECRAFT_BASH_TIMEOUT", 900);
	const ceiling = seconds("SWECRAFT_BASH_TIMEOUT_MAX", 0);
	pi.on("tool_call", (event) => {
		if (!isToolCallEventType("bash", event)) return;
		if (timeout > 0 && event.input.timeout === undefined) {
			event.input.timeout = timeout;
		}
		if (ceiling > 0 && typeof event.input.timeout === "number" && event.input.timeout > ceiling) {
			console.error(`[swecraft-bash] timeout ${event.input.timeout}s clamped to the ${ceiling}s ceiling`);
			event.input.timeout = ceiling;
		}
	});
}
