import {act, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {beforeEach, expect, it, vi} from 'vitest';
import {api} from '../api';
import {host} from '../test-fixtures';
import type {Context} from '../types';
import TagsPage from './TagsPage';

vi.mock('../api', async original => ({...await original<typeof import('../api')>(), api: vi.fn()}));
const mocked = vi.mocked(api);
const hosts = [host({tags: ['Production', 'client-a']}), host({id: 'host-b', alias: 'beta', tags: ['Staging']})];
let tags: Context[];
beforeEach(() => {
  tags = [{name: 'Production', count: 1}, {name: 'Staging', count: 1}];
  history.replaceState({}, '', '#/tags/Production');
  mocked.mockReset();
  mocked.mockImplementation(async (path, options) => {
    if (path === '/contexts' && !options?.method) return tags;
    if (options?.method === 'PATCH') {const body = JSON.parse(options.body as string); tags = [{name: body.new_name, count: body.server_ids.length}]; return tags[0];}
    if (options?.method === 'POST') {const body = JSON.parse(options.body as string); const tag = {name: body.name, count: body.server_ids.length}; tags.push(tag); return tag;}
    if (options?.method === 'DELETE') {tags = tags.filter(tag => path !== `/contexts/${encodeURIComponent(tag.name)}`); return {name: 'Production', removed_from: 1};}
    throw new Error('Unexpected operation');
  });
});
const mutations = () => mocked.mock.calls.filter(([, options]) => options?.method);

it('keeps membership filters and drafts local until Save, and Discard restores the original selection', async () => {
  render(<TagsPage hosts={hosts} onRefresh={vi.fn().mockResolvedValue(undefined)}/>);
  expect(await screen.findByRole('textbox', {name: 'Tag name'})).toHaveValue('Production');
  fireEvent.click(screen.getByRole('button', {name: 'Tagged'}));
  expect(screen.getByRole('checkbox', {name: 'Tag alpha'})).toBeChecked();
  expect(screen.queryByRole('checkbox', {name: 'Tag beta'})).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: 'Untagged'}));
  fireEvent.click(screen.getByRole('checkbox', {name: 'Tag beta'}));
  expect(screen.queryByRole('checkbox', {name: 'Tag beta'})).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: 'All'}));
  expect(screen.getByText('+ Adding')).toBeTruthy();
  fireEvent.change(screen.getByRole('textbox', {name: 'Tag name'}), {target: {value: 'Renamed'}});
  expect(mutations()).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', {name: 'Discard'}));
  expect(screen.getByRole('textbox', {name: 'Tag name'})).toHaveValue('Production');
  expect(screen.getByRole('checkbox', {name: 'Tag beta'})).not.toBeChecked();
  expect(mutations()).toHaveLength(0);
});

it('renames and replaces the complete membership in one request, preserving unrelated tags', async () => {
  const refreshed = vi.fn().mockResolvedValue(undefined);
  render(<TagsPage hosts={hosts} onRefresh={refreshed}/>);
  await screen.findByRole('textbox', {name: 'Tag name'});
  fireEvent.click(screen.getByRole('checkbox', {name: 'Tag alpha'}));
  fireEvent.click(screen.getByRole('checkbox', {name: 'Tag beta'}));
  fireEvent.change(screen.getByRole('textbox', {name: 'Tag name'}), {target: {value: 'Ops / East'}});
  fireEvent.click(screen.getByRole('button', {name: 'Save changes'}));
  await waitFor(() => expect(refreshed).toHaveBeenCalledOnce());
  expect(mutations()).toHaveLength(1);
  const [path, options] = mutations()[0];
  expect(path).toBe('/contexts/Production');
  expect(options?.method).toBe('PATCH');
  expect(JSON.parse(options?.body as string)).toEqual({new_name: 'Ops / East', server_ids: ['host-b']});
  expect(hosts[0].tags).toEqual(['Production', 'client-a']);
  await waitFor(() => expect(location.hash).toBe('#/tags/Ops%20%2F%20East'));
});

it('keeps the draft after a failed save and resolves hash selection before dialog query parameters', async () => {
  history.replaceState({}, '', '#/tags/Staging?dialog=unused');
  mocked.mockImplementation(async (_, options) => {if (options?.method) throw new Error('Name already exists'); return tags;});
  render(<TagsPage hosts={hosts} onRefresh={vi.fn()}/>);
  expect(await screen.findByRole('textbox', {name: 'Tag name'})).toHaveValue('Staging');
  fireEvent.change(screen.getByRole('textbox', {name: 'Tag name'}), {target: {value: 'Production'}});
  fireEvent.click(screen.getByRole('button', {name: 'Save changes'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('Name already exists');
  expect(screen.getByRole('textbox', {name: 'Tag name'})).toHaveValue('Production');
  await act(async () => {location.hash = '#/tags/Production'; window.dispatchEvent(new Event('hashchange'));});
  expect(screen.getByRole('checkbox', {name: 'Tag alpha'})).toBeChecked();
});

it('creates a tag with selected hosts and requires a separate confirmation before deleting it', async () => {
  render(<TagsPage hosts={hosts} onRefresh={vi.fn().mockResolvedValue(undefined)}/>);
  await screen.findByRole('textbox', {name: 'Tag name'});
  fireEvent.click(screen.getByRole('button', {name: 'New tag'}));
  const dialog = screen.getByRole('dialog', {name: 'New tag'});
  fireEvent.change(within(dialog).getByRole('textbox', {name: 'Tag name'}), {target: {value: 'Empty'}});
  fireEvent.click(within(dialog).getByRole('button', {name: 'Save tag'}));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(JSON.parse(mutations()[0][1]?.body as string)).toEqual({name: 'Empty', server_ids: []});
  await screen.findByRole('heading', {name: 'Empty'});
  fireEvent.click(screen.getByRole('button', {name: 'Delete tag…'}));
  expect(mutations()).toHaveLength(1);
  expect(screen.getByRole('dialog')).toHaveTextContent('hosts and their connection settings are preserved');
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {name: 'Delete tag'}));
  await waitFor(() => expect(mutations()).toHaveLength(2));
  expect(mutations()[1][1]?.method).toBe('DELETE');
});
