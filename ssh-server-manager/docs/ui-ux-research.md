# UI/UX research: host and tag management

Research date: 2026-07-16. Discovery and page extraction were performed with
AnySearch. This note records the evidence behind the interaction model rather
than treating visual polish as the primary fix.

## Product model

SSH Server Manager is a complex, expert-facing application: people scan a
large resource inventory, move between files and configuration, and perform
changes whose scope must be obvious. The interface should optimize repeated
work while keeping potentially destructive operations explicit.

## Evidence

- [NN/g: 8 Design Guidelines for Complex Applications](https://www.nngroup.com/articles/complex-application-design/)
  recommends flexible pathways, contextual accelerators, progressive
  disclosure, preserving the transition between primary and secondary
  information, and removing visual clutter without removing capability.
- [NN/g: usability heuristics for complex applications](https://www.nngroup.com/articles/usability-heuristics-complex-applications/)
  emphasizes visible system state, user control, recognition over recall, and
  efficient paths for experienced users.
- [PatternFly bulk selection](https://www.patternfly.org/patterns/bulk-selection)
  puts selection first, keeps a visible selected count, represents partial
  selection, and preserves the selection until the user clears it.
- [PatternFly toolbar guidance](https://www.patternfly.org/components/toolbar/design-guidelines/)
  orders bulk selection before filters and actions, and keeps item counts
  visible.
- [PatternFly filter guidance](https://www.patternfly.org/patterns/filters/design-guidelines/)
  recommends typeahead when the option set is large and removable labels when
  the active filter is otherwise hidden.
- [Carbon data-table guidance](https://carbondesignsystem.com/components/data-table/usage/)
  treats the table as the primary surface for resource comparison, search,
  selection, and batch actions. It advises against placing dense data tables in
  cramped modals.
- [AWS Tag Editor](https://docs.aws.amazon.com/tag-editor/latest/userguide/tagging-resources-add.html)
  uses a resource-first sequence: filter resources, select rows, manage tags,
  review scope, apply, and show success or failure.
- [GitLab bulk-update proposal](https://gitlab.com/gitlab-org/gitlab/-/work_items/492460)
  identifies the ambiguity of a single multiselect “update” and proposes
  separate add/remove actions with self-contained saves.
- [Linear label documentation](https://linear.app/docs/labels) separates the
  label library from item assignment, supports inline label creation during
  assignment, exposes usage data, and makes deletion deliberately explicit.
- [Termius host documentation](https://docs.termius.com/organize-and-connect-to-hosts)
  keeps the primary connection fields visible and reveals less-common host
  configuration behind a “Show more” step.

## Audit of the previous flow

1. A generic **Manage** button opened a modal with another two-pane resource
   browser, removing users from the Connections inventory they were organizing.
2. The workflow started from a tag and then asked users to reconstruct its
   entire host membership. This was slower for the common task, “put these
   hosts in this project.”
3. **Save assignments** did not communicate whether it added, removed, or
   replaced membership.
4. Tag chips displayed in the table were inert even though they looked
   interactive.
5. Decorative summary cards consumed vertical space above the actionable host
   list while offering no action.
6. Tag creation, taxonomy cleanup, filtering, and host assignment were
   mixed into one modal.

## Adopted interaction model

- **Connections is resource-first.** Search the inventory, select visible
  rows, then use one **Edit tags** picker. Existing tags are toggles,
  and a typed name can be created and assigned without leaving the picker.
- **Selection is durable and inspectable.** The count stays visible, select-all
  has checked/partial/empty states, and only **Clear selection** discards it.
- **Tags is a first-class workspace.** It provides search, inline creation
  and rename, usage counts, host previews, and an explicit two-step deletion.
- **Filtering is direct.** Clicking a tag in a host row immediately
  switches the inventory to that scope; the lightweight global selector remains
  available in Files and Connections.
- **Single-host editing stays local.** Every Tags-column action opens the same
  checked/mixed/empty picker used by bulk selection, and the Files sidebar
  exposes that picker beside every host. The host form keeps its autocomplete
  chips for full server editing.
- **Secondary controls are contextual.** Tag filters are hidden from
  Credentials and the Tags library. The old modal and non-actionable summary
  cards were removed.
- **Optional capability stays lazy.** Tag management, bulk selection, and
  the chip picker are loaded only when those workflows are opened.

## Visual hierarchy follow-up

A subsequent annotated review identified density and hierarchy problems that
were not solved by the task-model pass alone. The implementation now keeps the
page title visually dominant, uses an 8-pixel spacing rhythm and tighter corner
radii, and reduces Connections to five primary columns. Endpoint metadata is
grouped in one cell, status combines icon, color, and text, and secondary row
actions no longer compete with Browse. File rows use zebra scanning, persistent
headers, normalized modification labels, semantic access summaries, and an
explicit refresh action. Credential protection is now adjacent to the list it
governs, while tag creation sits in the Tags list header rather than in
a detached hero area. Empty workspaces offer direct host, add, and import paths.

## Follow-up opportunities

The next evidence-gathering step should be task-based usability observation,
not another aesthetic pass. Measure time and errors for: assigning 20 hosts to
a project, removing 3 hosts, renaming a tag, finding all unassigned hosts,
and recovering from an accidental deletion attempt. Likely later additions are
archive/undo, saved multi-tag views, and keyboard-driven tag switching;
they should be added only after those tasks show a real need.
