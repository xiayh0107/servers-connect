import {useEffect, useRef, useState} from 'react';
import {api, json, segment} from '../api';
import type {Host, TestResult} from '../types';

const FRESH_MS = 60_000, FAILURE_MS = 30_000;
type Observation = {host?: Host; until: number; failed?: boolean};
const identity = (host: Host) => JSON.stringify([host.id, host.hostname, host.port, host.username, host.credential_id, host.proxy_jumps]);

// A single flight per inventory keeps sweeping the pointer across rows from
// launching a fleet probe. Only the still-hovered host can start the next one.
export function usePreviewObservation(host: Host | undefined, refresh: () => Promise<void>, paused = false) {
  const cache = useRef(new Map<string, Observation>()), running = useRef(false);
  const alive = useRef(false), controller = useRef<AbortController | undefined>(undefined);
  const refreshRef = useRef(refresh); refreshRef.current = refresh;
  const [revision, setRevision] = useState(0);
  const key = host ? identity(host) : undefined;
  const observed = Date.parse(host?.facts?.observed_at || '');
  const fresh = Number.isFinite(observed) && Date.now() - observed >= 0 && Date.now() - observed < FRESH_MS;
  const cached = key ? cache.current.get(key) : undefined;
  const reusable = !!cached && cached.until > Date.now();

  useEffect(() => {
    alive.current = true;
    return () => {alive.current = false; controller.current?.abort();};
  }, []);
  useEffect(() => {
    if (!host || !key || paused || fresh || reusable || running.current) return;
    const abort = new AbortController(); controller.current = abort; running.current = true;
    void (async () => {
      try {
        const result = await api<TestResult>(`/hosts/${segment(host.id)}/observe`, {...json('POST', {timeout: 8}), signal: abort.signal});
        if (!result.ok) throw new Error('Observation failed');
        const updated = await api<Host>(`/hosts/${segment(host.id)}`, {signal: abort.signal});
        if (identity(updated) !== key) throw new Error('Host changed');
        if (!alive.current || abort.signal.aborted) return;
        cache.current.set(key, {host: updated, until: Date.now() + FRESH_MS});
        // A local inventory refresh failure must not discard collected facts.
        try {await refreshRef.current();} catch { /* The preview already has the new snapshot. */ }
      } catch {
        if (alive.current && !abort.signal.aborted) cache.current.set(key, {host: cached?.host, until: Date.now() + FAILURE_MS, failed: true});
      } finally {
        running.current = false;
        if (alive.current) setRevision(value => value + 1);
      }
    })();
  }, [key, fresh, reusable, paused, revision]);

  const latest = host && cached?.host && (!host.facts?.observed_at || (cached.host.facts?.observed_at || '') >= host.facts.observed_at) ? {...host, facts: cached.host.facts} : host;
  return {host: latest, loading: !!host && !fresh && !reusable && !paused, failed: !fresh && reusable && !!cached?.failed};
}
