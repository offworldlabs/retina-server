# Email node claiming

Date: 2026-09-17
Repo: retina-server
Status: design, awaiting review

Replaces claim codes with an email address the node collects and the server
verifies by magic link. Agreed in outline with Joshua on 2026-09-17.

Scope is the server side only. The node's setup UI, and its handling of the
signals described here, are Joshua's. The magic-link infrastructure is a separate
workstream and is treated as a dependency rather than designed here.

## Problem

Node ownership is an authorisation boundary rather than a label. `node_owners`
maps a node to a user, and that map decides what a caller is served:
`routes/analytics.py` adds the caller's own nodes to the public analytics
payload, and the owner websocket in `routes/streaming.py` filters unredacted
frames to owned nodes. Whatever writes `node_owners` decides who sees raw data.

Outside the admin routes, the only thing that writes it is a claim code: a
48-bit one-shot secret minted in `core/auth.py` against a signed-in user,
offered at `/api/auth/me/claim-codes`, and consumed from the legacy TCP `HELLO`
in `services/tcp_handler.py`. Three problems follow.

**No v1 path exists.** `contracts/nodes-v1.openapi.yaml` publishes register,
config, contact, detection and heartbeat. There is no claim operation, so a node
speaking v1 cannot be owned by any route.

**The funnel is long and inverted.** The user must create an account, sign in,
generate a code, write it into the node's configuration and restart, before the
node can announce itself. Holding a session is a precondition for starting,
which is the wrong way round for someone whose first contact with the product is
hardware in a box. The secret also ends up at rest in a configuration file on a
device that gets posted to people.

**There is no self-service release.** `routes/admin.py` can reassign an owner,
and the node-side path refuses to claim a node that already has one, so a
second-hand node needs an administrator.

## What replaces it

The node collects an email address during setup and sends it to the server. The
server mails that address a link. Clicking it verifies the address, creates an
account if there is not already one, binds the node to that account, and signs
the clicker in.

Email stays optional and nothing about node operation depends on it. A node with
no address, or with one that was never verified, runs exactly as it does now.
Mail is in the critical path for ownership, never for the node working.

### Why the click is sufficient proof

Two independent acts are required: someone holding the node nominates an
address, and someone holding that address accepts. Neither party alone can bind
the node. For an unowned node this is enough, because the only party who can
nominate is the one holding the hardware, and for an unowned node that person is
the legitimate authority on where it goes.

The proof is joint rather than held by one party, so it does not extend to a node
that already has an owner: there the nominator is not the authority, the
incumbent is. That case is covered by release, below, rather than by the
verification itself.

## Data model

The governing rule is that **email is the channel, not the key**. Verification
resolves an address to an account; the durable fact is the binding between node
and account.

The owner's address lives on its own resource, `/v1/nodes/claim`, rather than
inside the contact document. One address and one verification flag per node, as
agreed with Joshua; only the endpoint differs.

Keeping it in the contact document was tried and rejected. Four of that
document's five fields are one coherent record of a person to telephone, down to
`country` existing only to say where `phone` is dialable from, and an
authorisation input among them has different rules from all of its neighbours: it
cannot be freely rewritten, an omission must not clear it, and the endpoint's
published promise that it "grants nothing" stops being true. Every one of those
is an exception carved out of a document that otherwise needs none.

What remains in `node_contacts` is therefore the site contact, which is what it
already was: unverified, freely writable, wholesale-replaced, and granting
nothing. Its own `email` stays, because a node on someone else's roof has a host
worth mailing as well as ringing. For most nodes that address and the owner's
will be the same string, and the node's setup UI is the place to offer to copy
one into the other rather than the protocol.

| Fact | Lives | Means |
| --- | --- | --- |
| Owner address | per node, on `/v1/nodes/claim` | the address this node was claimed with |
| Verified | per node, beside it | that address has been confirmed |
| Owner | `node_owners` | which account this node belongs to |
| Site contact | `node_contacts` | whom to telephone about the hardware |

Only the third gates data. The verified flag answers whether an address has been
confirmed; the binding answers who owns the node. They are set by the same event
and will agree at first, but they are different facts with different lifetimes,
and the day something reads the flag to decide what to serve is the day they can
drift apart.

Keying ownership on the address itself was rejected. It makes ownership a string
comparison rather than a relation, so changing an address rewrites one row per
node, a set of nodes can only be known to be one person's by comparing strings,
and a recycled address silently grants. It also puts the per-node address and the
account's login address in two stores that have to agree.

### The node's address has a lifetime

```
unowned  ─── address verified ──▶  owned  ─── release ──▶  unowned
   │                                  │
address field is live              field is inert
(it routes the challenge)      (dashboard owns the address)
```

While the node is unowned the field is live and nominating an address starts a
claim. Once the node is owned the field has no further job: its token was spent
at the click. A release returns the node to the first state and the field becomes
live again.

The server enforces this rather than trusting the node to. While a node has an
owner, a write to `/v1/nodes/claim` nominating a different address is refused, and
the refusal carries the bound address so the node learns its own state from the
answer. Writing the address it already holds is accepted and changes nothing.

The site contact stays freely writable throughout, so a phone number can be
corrected without going near ownership.

An address can be offered at any point in a node's life. A node that was never
given one is not in a terminal state: it runs unowned indefinitely and can be
claimed whenever its owner gets round to it.

### One pending challenge per node

A node has at most one address awaiting verification. Nominating a new one
replaces it and invalidates whatever was outstanding, so a link mailed to a
mistyped address stops working the moment the address is corrected. Verification
therefore checks the token against the node's current pending address rather than
only checking that the token is unexpired and unused.

## Flows

**First node, new user.** Enter an address during setup, click the link, land in
the dashboard with an account created, the node bound and a session open. One
click does all three.

**Second node.** Enter the same address. The mail is a confirmation rather than a
signup, and one click adds the node to the existing account. The extra
confirmation is what stops a typo attaching hardware to someone else's account.

**Transfer.** The outgoing owner releases from the dashboard, which clears the
binding and returns the node to the unowned state. The incoming owner then claims
it normally. If the seller has not released, the buyer is told the node is
registered to someone else rather than being left with a silent no-op, so they
know who to chase.

**Account address change.** A pure account operation that touches no node. A
signed-in session nominates the new address, the server mails it a confirmation,
the click swaps it, and the outgoing address is notified. Every node follows
because none of them was ever bound to the string. Nothing about nodes appears in
this flow, and the node's stored address is not updated: it is a spent token
recording what the node was claimed with, not a pointer to the account.

Addresses retired this way stay attached to the account rather than being
released for reuse. Otherwise someone resetting a node later and typing an old
address from habit creates a second account holding that node.

## What the node is told

The node is never blocked. It sends an address and carries on, learning the
outcome from the server rather than waiting on it. There are three states to
render, unclaimed, pending verification and owned, alongside the address
currently in play.

A hard bounce is not a fourth. An address nobody can receive at leaves the node
exactly where an address nobody has clicked yet leaves it: unowned, with the
field live and a new address accepted normally. What differs is only what the
setup UI should say, so the bounce rides alongside the state as a flag on the
address rather than as a state of its own. It is carried, because "that address
does not exist" and "check your inbox" are different things to tell someone
standing in front of a node.

`HeartbeatResponse` is the carrier. The heartbeat goes every 60 seconds from
process start, unconditionally, which makes it the one channel that is live
whether or not anything else is. The contract already uses it exactly this way:
`node_ref` is documented there as the only place a node learns its public
identifier has rotated, and `config_stale` and `streaming_allowed` are repeated on
it so that a paused node still learns when it may resume. Ownership belongs in the
same place, and a release performed in the dashboard reaches the node by the same
route within a beat.

A minute is too long to leave a user staring at a wizard, so `/v1/nodes/claim`
answers a GET with the same state. Setup can poll it every few seconds while the
page is open, which is a short window with someone actively waiting at the end of
it, and stop once the answer settles. The two channels have different jobs: the
GET covers the minutes around setup, the heartbeat covers the years afterwards,
when a release performed in the dashboard has to reach a node that stopped
polling long ago.

There is still a window, up to one beat, in which a node can act on ownership
state that has already moved. It cannot be closed while the node does the asking,
only shortened. What makes it harmless is that the collision is answered rather
than merely refused: a node nominating an address for a node that has just become
owned is told so, and told which address won, in the same response.

Re-sending an address that has already been sent is a no-op. Asking for the mail
again is a separate, explicit action, because inferring intent from an ordinary
write would send mail on every configuration sync.

## Failure modes

| Case | Behaviour |
| --- | --- |
| No address given | Node runs unowned indefinitely. An address can be offered at any later point. |
| Mail never arrives or is never clicked | Node runs. Address on file unverified, grants nothing; the mail can be requested again. |
| Hard bounce | The outstanding challenge is dropped, so the node reads unclaimed again, and the address is flagged undeliverable so its UI can say the address does not exist rather than "check your inbox". Re-sending to that same address is refused; nominating a different one clears the flag and proceeds normally. |
| Link expires | Verification fails with a clear reason. The address stays on file and a fresh challenge can be requested. |
| Address typed wrongly and it belongs to someone real | The recipient gets a claim request naming the node, with a decline. Declining returns the node to unowned without support involvement. |
| Claimed by the wrong person and accepted | Release by that person, or an administrator. |
| Seller never released before selling | Buyer is told the node is already registered. Administrator can release. |
| Owner has lost access to their address | Account recovery, not a node problem: with magic-link login, losing the address is losing the account, so this inherits whatever recovery login has. |
| Node re-registers | Registration currently clears contact details. Re-registration must not clear the binding. |
| A node nominates an address for a node already owned | Refused, with the bound address in the answer, so the node reconciles from the refusal. |
| A second address nominated before the first is verified | The earlier challenge is invalidated; only the newest link works. |

There is deliberately no timeout that transfers an unresponsive owner's node to
whoever holds it. The signals available are identical for a vanished seller and a
thief, so any such rule is a theft path with a delay on it. An administrator is
the escape hatch.

## Abuse surface

Anyone who can get a node registered can make the server send mail to an address
of their choosing. This is new; claim codes had no such surface.

- Rate limit per address and per node, on both the first send and re-sends.
- The mail reads as a request naming the node, with a decline, not as a
  notification that assumes consent.
- The verification token carries its intent and the node id, and the landing page
  names the node before the click confirms. A claim link must not work as a bare
  login link for the sender's account if it is forwarded.
- Verification tokens are short-lived and single use.

## Mail infrastructure

Magic-link login makes sender reputation load-bearing for people signing in at
all, so this is worth doing once, properly, at the point the provider is
configured.

Capture bounces and complaints from the provider's feed. Without them,
`verified = false` is one state covering four situations that need different
responses: delivered and not yet clicked, sitting in a spam folder, a soft bounce
from a full mailbox, and a hard bounce from an address that does not exist. Only
the last is actionable, and knowing it turns a stored address into one that is
known to work, which is the distinction that matters when support is trying to
reach someone. Continuing to mail dead addresses and accumulating complaints is
also one of the main ways a sending domain's reputation degrades, and when it
does the failure is not confined to claiming.

## The magic-link dependency

The magic-link primitive is being built separately and is assumed present. This
design needs three things from it and nothing else:

- issue a challenge to an address, carrying an intent and the node id, and return
  a handle
- invalidate a previously issued challenge
- resolve a clicked challenge to an account, creating one if the address is new

Everything above is written against that surface, so it plugs in as the last step.
Until it exists the claim path can be exercised against a stub that records
challenges and resolves them on demand, which keeps the two workstreams
independent.

## Impact on this repo

**The contract.** A new path rather than an altered one, which is additive and
leaves existing generated clients working, so v1 needs no version bump. Two
response models gain a field. `/v1/nodes/contact` is untouched, description
included: with the owner's address elsewhere, its promise that what it stores "is
not an account, and it grants nothing" goes back to being true. The contract is
generated and CI-enforced, so it moves with the routes.

**Re-registration.** `routes/node_register.py` calls `delete_contact`, so a
re-register wipes the site contact. Both the binding and the verified owner
address must survive it, which they do by living outside that document.

**Removing claim codes.** The `claim_codes` table, the three routes under
`/api/auth/me/claim-codes`, the `HELLO` branch in `services/tcp_handler.py` and
the dashboard's onboarding page all go once this lands. The legacy TCP path is
the only place a code is consumed today, so removal is bounded. Existing
`node_owners` rows are unaffected: this changes how the row is written, not what
it means.

## Out of scope

Reconciling the site contact's address with the owner's. They are allowed to
differ indefinitely and nothing tries to keep them equal.

Authenticating registration. It is worth doing and it is tracked separately, but
it closes a different hole: a node proving it is a genuine node says nothing about
which person is behind it.
