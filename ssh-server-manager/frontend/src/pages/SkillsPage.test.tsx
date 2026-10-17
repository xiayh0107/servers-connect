import {act, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {beforeEach, expect, it, vi} from 'vitest';
import {api} from '../api';
import {host} from '../test-fixtures';
import type {Skill} from '../types';
import SkillsPage from './SkillsPage';

vi.mock('../api', async original => ({...await original<typeof import('../api')>(), api: vi.fn()}));
const mocked = vi.mocked(api);
const hosts = [host(), host({id: 'host-b', alias: 'beta'})];
const skill = (changes: Partial<Skill> = {}): Skill => ({id: 'skill-a', name: 'deploy-runbook', path: '/fixture/deploy/SKILL.md', description: 'Deployment instructions', status: 'ready', status_message: null, servers: [{id: 'host-a', alias: 'alpha'}], created_at: '2026-10-15T10:00:00Z', updated_at: '2026-10-15T10:00:00Z', ...changes});
let skills: Skill[];
beforeEach(() => {
  skills = [skill()]; history.replaceState({}, '', '#/skills/skill-a'); mocked.mockReset();
  mocked.mockImplementation(async (path, options) => {
    if (path === '/skills' && !options?.method) return skills;
    if (path.endsWith('/servers')) {const body = JSON.parse(options?.body as string); skills = [skill({servers: hosts.filter(host => body.server_ids.includes(host.id)).map(({id, alias}) => ({id, alias}))})]; return skills[0];}
    if (path.endsWith('/refresh')) return skills[0];
    if (path === '/skills' && options?.method === 'POST') {const body = JSON.parse(options.body as string); const value = skill({id: 'skill-new', name: 'new-skill', path: body.path, servers: []}); skills.push(value); return value;}
    if (options?.method === 'DELETE') {skills = []; return skill();}
    throw new Error('Unexpected operation');
  });
});
const mutations = () => mocked.mock.calls.filter(([, options]) => options?.method);

it('opens metadata without discovery or refresh and keeps assignments local until saved', async () => {
  render(<SkillsPage hosts={hosts}/>);
  await screen.findByRole('heading', {name: 'deploy-runbook'});
  expect(mocked.mock.calls.map(([path]) => path)).toEqual(['/skills']);
  expect(screen.getByText('Registered')).toBeTruthy();
  expect(screen.getByText(/Readable/)).toBeTruthy();
  fireEvent.click(screen.getByRole('checkbox', {name: 'Assign beta'}));
  expect(screen.getByText('+ Adding')).toBeTruthy();
  expect(screen.getByRole('button', {name: 'Refresh'})).toBeDisabled();
  expect(mutations()).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', {name: 'Discard'}));
  expect(screen.getByRole('checkbox', {name: 'Assign beta'})).not.toBeChecked();
  expect(mutations()).toHaveLength(0);
  fireEvent.click(screen.getByRole('checkbox', {name: 'Assign alpha'}));
  fireEvent.click(screen.getByRole('checkbox', {name: 'Assign beta'}));
  fireEvent.click(screen.getByRole('button', {name: 'Save assignments'}));
  await waitFor(() => expect(mutations()).toHaveLength(1));
  expect(mutations()[0][0]).toBe('/skills/skill-a/servers');
  expect(mutations()[0][1]?.method).toBe('PUT');
  expect(JSON.parse(mutations()[0][1]?.body as string)).toEqual({server_ids: ['host-b']});
  await waitFor(() => expect(screen.getByRole('checkbox', {name: 'Assign beta'})).toBeChecked());
});

it('refreshes metadata explicitly and searches assigned host aliases', async () => {
  render(<SkillsPage hosts={hosts}/>); await screen.findByRole('heading', {name: 'deploy-runbook'});
  fireEvent.change(screen.getByRole('textbox', {name: 'Search skills'}), {target: {value: 'alpha'}});
  expect(screen.getByRole('link', {name: /deploy-runbook/})).toBeTruthy();
  fireEvent.click(screen.getByRole('button', {name: 'Refresh'}));
  await waitFor(() => expect(mutations()).toHaveLength(1));
  expect(mutations()[0][0]).toBe('/skills/skill-a/refresh');
  expect(JSON.parse(mutations()[0][1]?.body as string)).toEqual({});
});

it('discovers only on request, refusing invalid, conflicting, or already registered candidates', async () => {
  const discovery = {candidates: [{name: 'available-skill', path: '/available/SKILL.md', status: 'available'}, {name: 'conflict-skill', path: '/conflict/SKILL.md', status: 'conflict'}, {path: '/invalid/SKILL.md', status: 'invalid', error: 'Invalid metadata'}, {name: 'known-skill', path: '/known/SKILL.md', status: 'registered', registered_id: 'skill-a'}], conflicts: [{type: 'name' as const, name: 'conflict-skill'}]};
  mocked.mockImplementation(async (path, options) => path === '/skills/discover' ? discovery : path === '/skills' && !options?.method ? skills : skill());
  render(<SkillsPage hosts={hosts}/>); await screen.findByRole('heading', {name: 'deploy-runbook'});
  expect(screen.queryByRole('dialog')).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: 'Discover'}));
  const dialog = await screen.findByRole('dialog', {name: 'Discovered local skills'});
  expect(mutations()).toHaveLength(0);
  expect(within(dialog).getAllByRole('button', {name: 'Register'}).map(button => button.hasAttribute('disabled'))).toEqual([false, true, true]);
  expect(within(dialog).getByRole('button', {name: 'Registered'})).toBeDisabled();
  fireEvent.click(within(dialog).getAllByRole('button', {name: 'Register'})[0]);
  await waitFor(() => expect(mutations()).toHaveLength(1));
  expect(JSON.parse(mutations()[0][1]?.body as string)).toEqual({path: '/available/SKILL.md', server_ids: []});
});

it('registers an exact path and shows the new skill without assigning hosts implicitly', async () => {
  render(<SkillsPage hosts={hosts}/>); await screen.findByRole('heading', {name: 'deploy-runbook'});
  fireEvent.click(screen.getByRole('button', {name: 'Register skill'}));
  const dialog = screen.getByRole('dialog', {name: 'Register local skill'});
  fireEvent.change(within(dialog).getByRole('textbox', {name: 'Local SKILL.md path'}), {target: {value: '/fixture/new/SKILL.md'}});
  fireEvent.click(within(dialog).getByRole('button', {name: 'Register'}));
  await screen.findByRole('heading', {name: 'new-skill'});
  expect(mutations()).toHaveLength(1);
  expect(JSON.parse(mutations()[0][1]?.body as string)).toEqual({path: '/fixture/new/SKILL.md', server_ids: []});
  expect(screen.getByRole('checkbox', {name: 'Assign alpha'})).not.toBeChecked();
});

it('requires removal of saved assignments before unregistering and preserves the local file', async () => {
  render(<SkillsPage hosts={hosts}/>); await screen.findByRole('heading', {name: 'deploy-runbook'});
  fireEvent.click(screen.getByRole('button', {name: 'Unregister…'}));
  expect(screen.queryByRole('button', {name: 'Remove registration'})).toBeNull();
  expect(mutations()).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', {name: 'Edit assignments'}));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  fireEvent.click(screen.getByRole('checkbox', {name: 'Assign alpha'}));
  fireEvent.click(screen.getByRole('button', {name: 'Save assignments'}));
  await waitFor(() => expect(screen.getByRole('button', {name: 'Save assignments'})).toBeDisabled());
  fireEvent.click(screen.getByRole('button', {name: 'Unregister…'}));
  expect(screen.getByRole('dialog')).toHaveTextContent('local SKILL.md file is preserved');
  fireEvent.click(screen.getByRole('button', {name: 'Remove registration'}));
  await waitFor(() => expect(mutations().at(-1)?.[1]?.method).toBe('DELETE'));
});

it('resolves hash queries and preserves assignment drafts after a rejected save', async () => {
  skills.push(skill({id: 'skill-b', name: 'broken', status: 'missing', status_message: 'Path is unavailable', servers: []}));
  history.replaceState({}, '', '#/skills/skill-b?dialog=unused');
  mocked.mockImplementation(async (_, options) => {if (options?.method) throw new Error('Host no longer exists'); return skills;});
  render(<SkillsPage hosts={hosts}/>); await screen.findByRole('heading', {name: 'broken'});
  expect(screen.getByText('Path is unavailable')).toBeTruthy();
  fireEvent.click(screen.getByRole('checkbox', {name: 'Assign beta'}));
  fireEvent.click(screen.getByRole('button', {name: 'Save assignments'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('Host no longer exists');
  expect(screen.getByRole('checkbox', {name: 'Assign beta'})).toBeChecked();
  await act(async () => {location.hash = '#/skills/skill-a'; window.dispatchEvent(new Event('hashchange'));});
  expect(screen.getByRole('heading', {name: 'deploy-runbook'})).toBeTruthy();
});
