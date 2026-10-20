# CMTP: Chess Move Transfer Protocol, Version 1

**Author:** Joseph Liga
**Status:** Final for version 1. Implemented by `src/cmtp` (protocol library), `src/server`, and `src/client`.

## Abstract

CMTP is an application-layer protocol for playing chess between two people on different computers,
through a server that holds the authoritative game. It runs over TCP. Each message is a JSON object
in a length-prefixed frame. The protocol covers connecting, finding and starting games, making moves,
offering draws, resigning, and recovering from a dropped connection without losing the game.

## Contents

1. [Introduction](#1-introduction)
2. [Conventions](#2-conventions)
3. [Transport](#3-transport)
4. [Framing](#4-framing)
5. [Message format](#5-message-format)
6. [Field types](#6-field-types)
7. [Messages](#7-messages)
8. [Connection and game states](#8-connection-and-game-states)
9. [Game rules](#9-game-rules)
10. [Errors](#10-errors)
11. [Heartbeats](#11-heartbeats)
12. [Dropped connections and resuming](#12-dropped-connections-and-resuming)
13. [Server shutdown and capacity](#13-server-shutdown-and-capacity)
14. [Versioning](#14-versioning)
15. [Security considerations](#15-security-considerations)
16. [Example exchanges](#16-example-exchanges)
17. [A frame, byte by byte](#17-a-frame-byte-by-byte)
18. [Constants](#18-constants)

## 1. Introduction

**Design goals:**

- **Correct over clever.** The server holds the only true copy of each game. Clients propose moves;
  the server decides. A client's board changes only when the server says a move happened.
- **Nothing lost or garbled.** TCP delivers bytes reliably and in order, but not in message-sized
  pieces. CMTP's framing turns the byte stream back into whole messages, and strict validation makes
  sure a damaged or wrong message is reported, never half-understood.
- **Survive real networks.** Heartbeats notice dead connections that TCP would miss for minutes, and
  a player whose connection drops can come back to the same game.
- **Easy to read and debug.** Messages are JSON, so they can be read in a packet capture or a log.

**Roles:**

- **Server:** accepts connections, keeps the lobby of open games, checks every move against the rules
  of chess, and tells both players what happened.
- **Client:** one per player. Shows the board, sends the player's requests, and displays the
  server's answers.

## 2. Conventions

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are used as described in
RFC 2119.

**Terms:**

- **Connection:** one TCP connection between a client and the server.
- **Session:** a player's identity on the server, created by `HELLO`. A session can outlive its
  connection (see section 12).
- **Game:** one chess game, from the moment it's created in the lobby until it ends.
- **Ply:** a half-move, one player's move. The first move of a game is ply 0.
- **Sender:** the side that sent a message. Every message type is sent by only the client or only
  the server.

**Notation:**

- Byte values are hexadecimal.
- Multi-byte integers are big-endian (network byte order).
- Chess positions are in FEN, and moves are in UCI long algebraic notation (section 6).

## 3. Transport

- CMTP runs over **TCP**. The default server port is **5328**.
- Each client uses **one connection** at a time. Every message travels on that connection.
- Both sides **SHOULD** turn off Nagle's algorithm (`TCP_NODELAY`). CMTP messages are small and need
  to go immediately; Nagle's algorithm would hold them back to batch them.
- CMTP has no encryption of its own. See section 15.

## 4. Framing

Every message is sent as one frame:

```
+--------------------------+----------------------------------+
| length: 4 bytes, unsigned | payload: exactly `length` bytes  |
| integer, big-endian       | (one UTF-8 JSON object)          |
+--------------------------+----------------------------------+
```

**Requirements:**

- `length` counts the payload only, not the 4 length bytes.
- The largest allowed payload is **65,536 bytes (64 KiB)**.
- A receiver **MUST** buffer bytes until a whole frame has arrived. TCP can deliver one frame split
  across many reads, or several frames, and part of another, in one read.
- If a length is over the limit, the receiver **MUST** treat the stream as broken. It doesn't wait
  for the payload: it **SHOULD** send `ERROR` 400 and **MUST** then close the connection. After a bad
  length there's no way to find where the next frame starts.
- A zero-length frame is valid framing, but its empty payload isn't a valid message (error 401).
- If the connection closes partway through a frame, the partial frame **MUST** be discarded.

**Why a length prefix:** the alternatives were a delimiter (such as a newline after each JSON
message) and fixed-size records.
- **Versus a delimiter:** a length prefix lets the receiver know how much to read, never has to scan
  for or escape a special character, and lets an oversized message be rejected before any of it is
  read.
- **Versus fixed-size records:** these would waste space, since CMTP messages vary from about 50 to
  over 1,000 bytes.

## 5. Message format

### 5.1 Envelope

The payload of every frame is a UTF-8 JSON object, the **envelope**:

```json client
{"v": 1, "type": "MOVE", "seq": 4, "re": null, "body": {"game_id": "g1", "ply": 0, "move": "e2e4"}}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `v` | integer | yes | Protocol version. This document is version **1**. |
| `type` | string | yes | The message type: one of the names in section 7. |
| `seq` | integer, at least 1 | yes | The sender's message number on this connection. |
| `re` | integer at least 1, or `null` | no | The `seq` of the message this one answers. Left out or `null` if it answers nothing. |
| `body` | object | yes | The fields for this message type, listed in section 7. Can be empty (`{}`). |

**Sequence numbers:**
- Each side numbers its own messages 1, 2, 3, and so on, and starts again at 1 on every new
  connection.
- `seq` lets a reply point back at the message it answers, through `re`. That's how a client knows
  which request an `ERROR` or `MOVE_REJECTED` is about.

### 5.2 Validation

Both sides **MUST** check every message they receive, and **SHOULD** check every message they send
(so a bug shows up on the side that has it). A message is rejected if any of these is true:

| Problem | Error code |
|---|---|
| The payload isn't valid UTF-8 | 401 |
| The payload isn't valid JSON, has a duplicate key, uses `NaN` or `Infinity`, or is nested too deeply to parse | 401 |
| The payload isn't a JSON object | 402 |
| `v` is missing or isn't an integer | 402 |
| `v` is an integer other than 1 | 101 (fatal) |
| An envelope field is missing, has the wrong type, or there's an extra field | 402 |
| `type` isn't a message type, or the sender isn't allowed to send it | 403 |
| A body field is missing, has the wrong type or an invalid value, or there's an extra field | 402 |

- **Order of checks:** they run in the order of the table. In particular, `v` is checked before the
  rest of the envelope, so a future version with a different envelope gets a clear version error.
- **Integers:** a JSON `true` or `false` is never accepted where an integer is expected, and `1.0` is
  not an integer.
- **Unknown fields:** fields not listed in section 7 are an error, not something to ignore.

## 6. Field types

| Name | JSON type | Rule |
|---|---|---|
| name | string | 1 to 32 characters, no control characters |
| game ID | string | 1 to 64 characters, chosen by the server |
| session ID, resume token | string | 1 to 128 characters, chosen by the server |
| color | string | `"white"` or `"black"` |
| ply | integer | 0 or more |
| UCI move | string | `[a-h][1-8][a-h][1-8]` and, for a promotion, one of `q r b n`. Example: `e2e4`, `e7e8q` |
| SAN | string | 1 to 10 characters. Standard Algebraic Notation, for showing to people. Example: `Nf3`, `exd5`, `O-O`, `e8=Q+` |
| FEN | string | 1 to 100 characters. A position in Forsyth-Edwards Notation, all six fields |
| result | string | `"1-0"` (white won), `"0-1"` (black won), or `"1/2-1/2"` (draw) |
| reason | string | how a game ended; see `GAME_OVER` |
| error code | integer | 100 to 599; see section 10 |
| text | string | 1 to 500 characters, no control characters |

**Why moves are UCI but the server also sends SAN:**
- **UCI for moves:** it's the same no matter what the position is, and trivial to check for format.
- **SAN for people:** it's what players expect to read, but writing it needs the position.
- **The split:** clients send UCI, and the server, which knows the position, adds SAN to every
  `MOVE_MADE`.
- **FEN in every update:** a client can compare its board with the server's after each move.

## 7. Messages

Every body field is required unless marked optional.

### 7.1 Client to server

#### `HELLO`

Opens a session. It **MUST** be the first message on a connection.

| Field | Type | Meaning |
|---|---|---|
| `name` | name | The player's display name. Names don't have to be unique. |
| `resume_token` | resume token, optional | Take back a session whose connection dropped (section 12). |

```json client
{"v": 1, "type": "HELLO", "seq": 1, "re": null, "body": {"name": "Ana"}}
```

```json client
{"v": 1, "type": "HELLO", "seq": 1, "re": null, "body": {"name": "Ana", "resume_token": "tN3x9Qk2VbLr8sJcYw1aFg"}}
```

**Replies:**
- **Success:** `WELCOME`.
- **Errors:** `ERROR` 102 if this connection has already sent `HELLO`; 103 if the resume token isn't
  valid.

#### `LIST_GAMES`

Asks for the games waiting for an opponent. The body is empty. Reply: `GAME_LIST`.

```json client
{"v": 1, "type": "LIST_GAMES", "seq": 2, "re": null, "body": {}}
```

#### `CREATE_GAME`

Opens a new game in the lobby and waits for an opponent.

| Field | Type | Meaning |
|---|---|---|
| `color` | color, optional | The color the creator wants. Left out: decided at random when someone joins. |

```json client
{"v": 1, "type": "CREATE_GAME", "seq": 3, "re": null, "body": {"color": "white"}}
```

**Replies:**
- **Success:** `GAME_CREATED`.
- **Errors:** `ERROR` 202 if the player already has a game that is waiting or under way.

#### `JOIN_GAME`

Joins a game from the lobby, which starts it.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game to join. |

```json client
{"v": 1, "type": "JOIN_GAME", "seq": 2, "re": null, "body": {"game_id": "g1"}}
```

**Replies:**
- **Success:** `GAME_START` to both players.
- **Errors**, checked in this order:
  1. `ERROR` 200 if there's no such game.
  2. 204 if it's the player's own game.
  3. 202 if the player already has a game.
  4. 201 if the game has already started.

#### `CANCEL_GAME`

Withdraws a game the player created, before anyone joins.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game to cancel. |

```json client
{"v": 1, "type": "CANCEL_GAME", "seq": 4, "re": null, "body": {"game_id": "g1"}}
```

**Replies:**
- **Success:** `GAME_CANCELLED`.
- **Errors:** `ERROR` 203 if it isn't the player's game; 206 if it has already started (resign
  instead).

#### `MOVE`

Proposes a move.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game. |
| `ply` | ply | The number of half-moves already played. The first move of the game has ply 0. |
| `move` | UCI move | The move. |

```json client
{"v": 1, "type": "MOVE", "seq": 4, "re": null, "body": {"game_id": "g1", "ply": 0, "move": "e2e4"}}
```

**Replies:**
- **Success:** `MOVE_MADE` to both players, then `GAME_OVER` if the move ended the game.
- **Any problem with a move that decoded correctly:** `MOVE_REJECTED`, never `ERROR`. The checks run
  in the order of section 9.1.

#### `RESIGN`

Gives up the game.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game. |

```json client
{"v": 1, "type": "RESIGN", "seq": 9, "re": null, "body": {"game_id": "g1"}}
```

**Replies:**
- **Success:** `GAME_OVER` to both players.
- **Errors:** `ERROR` 203, 205, or 303 (section 9.1).

#### `OFFER_DRAW`

Offers the opponent a draw.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game. |

```json client
{"v": 1, "type": "OFFER_DRAW", "seq": 7, "re": null, "body": {"game_id": "g1"}}
```

**Replies:**
- **Usually:** `DRAW_OFFERED` to the opponent. The offerer gets no reply.
- **If the opponent had already offered a draw:** this accepts it, and both players get `GAME_OVER`.
- **If this player already has an offer waiting:** nothing happens.
- **Errors:** `ERROR` 203, 205, or 303 (section 9.1).

#### `ANSWER_DRAW`

Answers the opponent's draw offer.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game. |
| `accept` | boolean | `true` to agree a draw, `false` to decline. |

```json client
{"v": 1, "type": "ANSWER_DRAW", "seq": 8, "re": null, "body": {"game_id": "g1", "accept": true}}
```

**Replies:**
- **Accepted:** `GAME_OVER` to both players.
- **Declined:** `DRAW_DECLINED` to the offerer.
- **Errors:** `ERROR` 304 if there's no offer from the opponent waiting, and 203, 205, or 303 as in
  section 9.1.

#### `GET_STATE`

Asks for the full state of the player's game, to rebuild a client's board. It works for a game that
has ended, too.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game. |

```json client
{"v": 1, "type": "GET_STATE", "seq": 12, "re": null, "body": {"game_id": "g1"}}
```

**Replies:**
- **Success:** `STATE`.
- **Errors:** `ERROR` 203 if it isn't the player's current or most recent game; 205 if the game
  hasn't started.

#### `PING`

A heartbeat (section 11). The body is empty. Reply: `PONG`.

```json client
{"v": 1, "type": "PING", "seq": 15, "re": null, "body": {}}
```

#### `BYE`

The client is leaving on purpose. The body is empty.
- **When the server receives it:** it closes the connection.
- **Mid-game:** the player forfeits (section 12).
- **Before `HELLO`:** `BYE` is accepted at any time, including then.

```json client
{"v": 1, "type": "BYE", "seq": 30, "re": null, "body": {}}
```

### 7.2 Server to client

#### `WELCOME`

Accepts a `HELLO`.

| Field | Type | Meaning |
|---|---|---|
| `session_id` | session ID | The player's session. |
| `resume_token` | resume token | A secret for resuming this session (section 12). Each one works once. |

```json server
{"v": 1, "type": "WELCOME", "seq": 1, "re": 1, "body": {"session_id": "s2", "resume_token": "tN3x9Qk2VbLr8sJcYw1aFg"}}
```

#### `GAME_LIST`

Replies to `LIST_GAMES` with the games waiting for an opponent.

| Field | Type | Meaning |
|---|---|---|
| `games` | list of objects | Each has `game_id` (game ID), `host` (name), and `host_color` (color, or `null` if random). |

```json server
{"v": 1, "type": "GAME_LIST", "seq": 2, "re": 2, "body": {"games": [{"game_id": "g1", "host": "Ana", "host_color": "white"}, {"game_id": "g2", "host": "Cy", "host_color": null}]}}
```

#### `GAME_CREATED`

Replies to `CREATE_GAME`. The creator now waits for an opponent.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The new game. |
| `color` | color or `null` | The creator's color, or `null` if it will be random. |

```json server
{"v": 1, "type": "GAME_CREATED", "seq": 3, "re": 3, "body": {"game_id": "g1", "color": "white"}}
```

#### `GAME_CANCELLED`

Replies to `CANCEL_GAME`. The game is gone from the lobby.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The cancelled game. |

```json server
{"v": 1, "type": "GAME_CANCELLED", "seq": 4, "re": 4, "body": {"game_id": "g1"}}
```

#### `GAME_START`

Sent to both players when a game starts. For the player who joined, `re` is their `JOIN_GAME`; for
the creator, it's `null`.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game. |
| `your_color` | color | This player's color. |
| `opponent` | name | The other player's name. |
| `fen` | FEN | The starting position. Version 1 always uses the standard starting position. |

```json server
{"v": 1, "type": "GAME_START", "seq": 3, "re": 2, "body": {"game_id": "g1", "your_color": "black", "opponent": "Ana", "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"}}
```

#### `MOVE_MADE`

Sent to **both** players when a move is played. This is the only confirmation that a move
happened. A client **MUST NOT** change its board for its own move until this arrives. For the
mover, `re` is their `MOVE`; for the opponent, it's `null`.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game. |
| `ply` | ply | The move's ply, the same as in the `MOVE`. |
| `move` | UCI move | The move. |
| `san` | SAN | The move in SAN, written by the server. |
| `fen` | FEN | The position after the move. |
| `by` | color | Who moved. |

```json server
{"v": 1, "type": "MOVE_MADE", "seq": 4, "re": 4, "body": {"game_id": "g1", "ply": 0, "move": "e2e4", "san": "e4", "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1", "by": "white"}}
```

#### `MOVE_REJECTED`

Replies to a `MOVE` that wasn't played. The game is unchanged.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game named in the `MOVE`. |
| `code` | error code | Why (section 10). |
| `reason` | text | A short explanation for people. |

```json server
{"v": 1, "type": "MOVE_REJECTED", "seq": 6, "re": 5, "body": {"game_id": "g1", "code": 301, "reason": "it is white's turn"}}
```

#### `DRAW_OFFERED`

The opponent offers a draw.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game. |

```json server
{"v": 1, "type": "DRAW_OFFERED", "seq": 8, "re": null, "body": {"game_id": "g1"}}
```

#### `DRAW_DECLINED`

The opponent turned down this player's draw offer: with `ANSWER_DRAW`, or by making a move instead.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game. |

```json server
{"v": 1, "type": "DRAW_DECLINED", "seq": 9, "re": null, "body": {"game_id": "g1"}}
```

#### `GAME_OVER`

The game has ended. Sent to both players.

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game. |
| `result` | result | `"1-0"`, `"0-1"`, or `"1/2-1/2"`. |
| `reason` | string | One of the reasons below. |

**Reasons:**

| Reason | Result | Meaning |
|---|---|---|
| `checkmate` | a win | The side to move is in check with no legal move. |
| `stalemate` | draw | The side to move has no legal move but isn't in check. |
| `insufficient_material` | draw | Neither side can ever checkmate. |
| `fifty_move` | draw | 50 moves by each side with no capture or pawn move. |
| `threefold_repetition` | draw | The same position for the third time. |
| `resignation` | a win | A player resigned. |
| `agreement` | draw | Both players agreed a draw. |
| `abandoned` | a win | A player left mid-game, or didn't come back in time (section 12). |

```json server
{"v": 1, "type": "GAME_OVER", "seq": 12, "re": null, "body": {"game_id": "g1", "result": "1-0", "reason": "checkmate"}}
```

#### `OPPONENT_STATUS`

The opponent's connection dropped (`connected` is `false`) or they came back (`true`).

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game. |
| `connected` | boolean | Whether the opponent is connected. |

```json server
{"v": 1, "type": "OPPONENT_STATUS", "seq": 7, "re": null, "body": {"game_id": "g1", "connected": false}}
```

#### `STATE`

The full state of a game: the reply to `GET_STATE`, and part of resuming (section 12).

| Field | Type | Meaning |
|---|---|---|
| `game_id` | game ID | The game. |
| `your_color` | color | This player's color. |
| `opponent` | name | The other player's name. |
| `moves` | list of UCI moves | Every move so far, in order, from the starting position. |
| `fen` | FEN | The current position. |
| `turn` | color | Whose move it is. |

```json server
{"v": 1, "type": "STATE", "seq": 2, "re": null, "body": {"game_id": "g1", "your_color": "white", "opponent": "Ben", "moves": ["e2e4", "e7e5"], "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2", "turn": "white"}}
```

#### `ERROR`

A request was refused, or a message couldn't be understood. `re` is the `seq` of the message
responsible, or `null` if it couldn't be read.

| Field | Type | Meaning |
|---|---|---|
| `code` | error code | What went wrong (section 10). |
| `message` | text | A short explanation for people. |

```json server
{"v": 1, "type": "ERROR", "seq": 5, "re": 7, "body": {"code": 402, "message": "MOVE body: missing field 'ply'"}}
```

#### `PONG`

Replies to `PING`. The body is empty.

```json server
{"v": 1, "type": "PONG", "seq": 16, "re": 15, "body": {}}
```

## 8. Connection and game states

### 8.1 A connection, as the server sees it

```
             HELLO                CREATE_GAME
  CONNECTED ───────► IN_LOBBY ◄───────────────► HOSTING
      │                │   ▲   CANCEL_GAME         │
      │   JOIN_GAME    │   │                       │ someone joins
      │                ▼   │ GAME_OVER             ▼
      │              PLAYING ◄─────────────────────┘
      │                │
      │                │ connection drops without BYE
      │                ▼
      │              HELD ───── resumed (section 12) ─────► PLAYING
      │                │
      │                │ resume window ends
      ▼                ▼
   CLOSED ◄──── BYE, connection closed, fatal error ──── (any state)
```

**Rules:**
- **Before `HELLO`:** any other message gets `ERROR` 100. `BYE` is the exception.
- **Leaving a waiting game:** a `HOSTING` player whose connection ends loses the open game, which is
  removed from the lobby. Seats are held only for games under way.
- **After `GAME_OVER`:** the player is back `IN_LOBBY`. The finished game stays their "most recent
  game" for `GET_STATE` until they create or join another.

### 8.2 A game

```
  OPEN ──(join)──► WHITE_TO_MOVE ⇄ BLACK_TO_MOVE ──(GAME_OVER)──► FINISHED
   │
   └──(cancel, or the creator leaves)──► removed
```

- **While a player's seat is held**, the game is suspended for them. Their opponent may still move,
  resign, or offer a draw.
- **When the game ends**, both players get `GAME_OVER`. A player whose seat is held gets it when they
  resume.

## 9. Game rules

### 9.1 How the server checks a move

The server applies these checks in order. The first one that fails decides the code in
`MOVE_REJECTED`.

1. **The game is the player's current game:** otherwise 203 (`NOT_IN_GAME`).
2. **It has started:** otherwise 205 (`GAME_NOT_STARTED`).
3. **It isn't over:** otherwise 303 (`GAME_ALREADY_OVER`).
4. **`ply` equals the number of half-moves played so far:** otherwise 302 (`STALE_PLY`).
5. **It's the player's turn:** otherwise 301 (`NOT_YOUR_TURN`).
6. **The move is legal:** otherwise 300 (`ILLEGAL_MOVE`).

The ply is checked before the turn on purpose. If a client sends the same move twice (a bug, or a
retry after a reconnect), the copy is reported as stale, not as out of turn, which tells the client
what really happened. `RESIGN`, `OFFER_DRAW`, and `ANSWER_DRAW` use checks 1 to 3, answered with
`ERROR`.

### 9.2 Legal moves

- Moves follow the FIDE Laws of Chess, including castling, en passant, and promotion.
- A promotion **MUST** name its piece. `e7e8` is illegal; `e7e8q` is a queen.
- Castling is the king's move: `e1g1`, `e1c1`, `e8g8`, `e8c8`.

### 9.3 Draws

- **Automatic draws:** the server declares stalemate, insufficient material, the fifty-move rule,
  and threefold repetition by itself, so the protocol has no "claim draw" message.
- **Checkmate comes first:** a move that checkmates wins even if it also reaches the fifty-move
  limit.
- **Repetition:** a position counts as repeated when the pieces, the side to move, and castling
  rights are the same. The en passant square counts too, but only when an en passant capture is
  actually possible.
- **Draw offers** stay open until the opponent accepts or declines. Making a move instead of
  answering declines the offer (the offerer gets `DRAW_DECLINED` after the `MOVE_MADE`). Offering a
  draw back when the opponent has one waiting accepts it.

## 10. Errors

Codes are grouped by hundreds, like HTTP status codes.

- **Which message carries them:** a refused `MOVE` gets `MOVE_REJECTED`; everything else gets
  `ERROR`.
- **Fatal codes:** after sending a fatal code, the sender **MUST** close the connection. After any
  other code the connection stays open.

| Code | Name | When | Fatal |
|---:|---|---|---|
| 100 | `HELLO_REQUIRED` | A message other than `HELLO` arrived before `HELLO`. | |
| 101 | `UNSUPPORTED_VERSION` | `v` isn't a version the receiver speaks. | yes |
| 102 | `ALREADY_GREETED` | `HELLO` was sent twice on one connection. | |
| 103 | `BAD_RESUME_TOKEN` | Nothing to resume with that token. The client may send `HELLO` without one. | |
| 200 | `GAME_NOT_FOUND` | There's no game with that ID in the lobby. | |
| 201 | `GAME_FULL` | The game already has two players. | |
| 202 | `ALREADY_IN_GAME` | The player already has a game waiting or under way. | |
| 203 | `NOT_IN_GAME` | The player isn't in the game named. | |
| 204 | `CANNOT_JOIN_OWN_GAME` | A player tried to join the game they created. | |
| 205 | `GAME_NOT_STARTED` | The game is still waiting for an opponent. | |
| 206 | `GAME_ALREADY_STARTED` | Too late to cancel; resign instead. | |
| 300 | `ILLEGAL_MOVE` | The move isn't legal in the current position. | |
| 301 | `NOT_YOUR_TURN` | It's the opponent's turn. | |
| 302 | `STALE_PLY` | The move's `ply` doesn't match the game. | |
| 303 | `GAME_ALREADY_OVER` | The game has ended. | |
| 304 | `NO_DRAW_OFFER` | `ANSWER_DRAW` with no offer from the opponent waiting. | |
| 400 | `FRAME_TOO_LARGE` | A frame's length is over 64 KiB. | yes |
| 401 | `MALFORMED_JSON` | The payload isn't valid UTF-8 JSON (section 5.2). | |
| 402 | `INVALID_MESSAGE` | Valid JSON, but the envelope or body breaks the rules. | |
| 403 | `UNKNOWN_TYPE` | Unknown message type, or one the sender isn't allowed to send. | |
| 500 | `INTERNAL_ERROR` | A bug on the server while handling the message. | |
| 501 | `SERVER_FULL` | Too many connections. Sent right after connecting, before `HELLO`. | yes |
| 502 | `SHUTTING_DOWN` | The server is stopping. | yes |

- **Errors don't break the connection.** A malformed message is reported with `ERROR` and skipped.
  Framing is still intact, so the next message is read normally. That's why one bad message doesn't
  end the connection unless its code is fatal.
- **Text is for people:** a client **SHOULD NOT** parse the `message` or `reason` text. The code is
  what programs act on.

## 11. Heartbeats

- **Clients ping:** a client **SHOULD** send `PING` every **10 seconds**.
- **The server times out silent clients:** if it receives no complete frame for **30 seconds**, it
  **MAY** treat the connection as lost (section 12).
- **Clients time out a silent server:** if a client receives nothing at all for **30 seconds**, it
  **SHOULD** do the same.

**Why heartbeats:** TCP notices a connection is dead only when it tries to send and gets no
acknowledgment. With nothing being sent (a player thinking about a move, a laptop lid closed, Wi-Fi
gone), that can take many minutes. Heartbeats bound the wait.

**Round-trip time:** a client can measure it from its `PING` and the matching `PONG`. It need not show
heartbeat `PONG`s to the player.

## 12. Dropped connections and resuming

### 12.1 Leaving vs. dropping out

- **`BYE`:** the player is leaving on purpose. If a game is under way, it ends at once: the opponent
  wins with reason `abandoned`.
- **A connection that ends without `BYE`:** includes a reset, a timeout (section 11), or a fatal
  protocol error. Mid-game, this looks like a network failure, so the server **holds the player's
  seat** for the **resume window, 60 seconds**:
  - The opponent gets `OPPONENT_STATUS` with `connected: false`.
  - The game goes on. The opponent may move, resign, or offer a draw.
  - If the player hasn't resumed when the window ends, the game ends as `abandoned` and the opponent
    gets `GAME_OVER`.
- **Not mid-game:** a player in the lobby has nothing to hold. Their session ends, and their open
  game, if they had one, is removed.

### 12.2 Resuming

A client resumes by connecting again and sending `HELLO` with the `resume_token` from its most recent
`WELCOME`. The `name` field is still required but is ignored: the session keeps its name. The server
then:

1. **Restores the session.** The new connection gets the same session ID, and the player gets their
   seat back.
2. **Replaces the old token.** It sends `WELCOME` with the same `session_id` and a **new**
   `resume_token`; the old token stops working. A token that has been used, or a stolen copy of it,
   can't be used again.
3. **Sends the state, if the player's game has started:** `STATE`, then
   - `GAME_OVER` if the game ended while the player was away, or
   - `DRAW_OFFERED` if the opponent has a draw offer waiting.
4. **Tells the opponent:** `OPPONENT_STATUS` with `connected: true`, if the seat had been held.

**Other cases:**
- **The server hadn't noticed the old connection was dead** (common: the client's network failed,
  and the server is still waiting for the next heartbeat). The new connection replaces the old one,
  which the server closes. The opponent sees nothing, because nothing was interrupted from their
  side.
- **The token isn't valid** (never issued, already used, the window ended, or the player was only in
  the lobby): `ERROR` 103. The connection is still open and not yet greeted, so the client can send
  `HELLO` without a token and start a new session.

### 12.3 What a client does

A client **SHOULD**:

- reconnect and resume by itself when a connection drops unexpectedly, retrying for up to the resume
  window;
- not reconnect after `BYE`, `ERROR` 501, `ERROR` 502, or a message from the server it couldn't
  decode;
- treat the `STATE` after resuming as the truth, and replace its board with it. A move it sent just
  before the drop may or may not have been played; `STATE` says which.

## 13. Server shutdown and capacity

- **Shutdown:** when the server stops, it sends every client `ERROR` 502, then closes every
  connection. No seats are held, since there's nothing to come back to.
- **Capacity:** a server **MAY** limit how many connections it accepts. A connection over the limit
  gets `ERROR` 501 straight away, then is closed.
- **Slow readers:** a server **MAY** close a connection whose unsent outgoing data grows too large (the
  reference server allows 1 MiB). A client that stops reading must not use up the server's memory.

## 14. Versioning

- **Every message carries `v`:** this document defines version **1**.
- **Unsupported versions:** a receiver that gets a version it doesn't speak **MUST** reply `ERROR`
  101 and close the connection. The error message says which version it does speak.
- **Future versions:** they may change anything except where `v` is: a top-level integer field of
  the JSON object in each frame. That way, any version can recognize any other.

## 15. Security considerations

CMTP version 1 is designed for a classroom or a trusted network.

- **No encryption.** Everything, including resume tokens, crosses the network in plain text. Anyone
  on the path can read games or steal a resume token and take over a session. Running CMTP inside TLS
  would fix both, with no change to the messages.
- **No accounts.** Names aren't checked or reserved; two players can use the same name. The only
  secret is the resume token: 128 random bits from a cryptographic random source, valid once, and
  only while a seat is held or the session is connected.
- **Memory limits:** a peer can't make the receiver hold more than one 64 KiB frame. Oversized
  lengths are rejected as soon as the 4-byte header arrives.
- **Hostile input:** every message is validated against a fixed schema before use. Duplicate keys,
  `NaN`, and JSON nested deeply enough to crash a parser are all rejected with errors, not crashes.
  The reference server survived a fuzzing run of 235 random and corrupted inputs.
- **Holding resources:** idle connections time out after 30 seconds, and a server can cap total
  connections (error 501). There's no per-client rate limit in version 1; a client sending
  thousands of valid messages per second would use server CPU.
- **Server authority:** clients can't change a game except through moves the server checks.

## 16. Example exchanges

`→` is a message to the server and `←` is one from it. Each line shows `seq`, then `re` where set.

### 16.1 Two players start a game and play two moves

```
Ana (white)                            Server                              Ben (black)
  │ → HELLO seq=1 {name: Ana}            │                                     │
  │ ← WELCOME seq=1 re=1                 │                                     │
  │ → CREATE_GAME seq=2 {color: white}   │                                     │
  │ ← GAME_CREATED seq=2 re=2 {g1}       │                                     │
  │                                      │ → HELLO seq=1 {name: Ben}           │
  │                                      │ ← WELCOME seq=1 re=1                │
  │                                      │ → LIST_GAMES seq=2                  │
  │                                      │ ← GAME_LIST seq=2 re=2 [g1]         │
  │                                      │ → JOIN_GAME seq=3 {g1}              │
  │ ← GAME_START seq=3 (white)           │ ← GAME_START seq=3 re=3 (black)     │
  │ → MOVE seq=3 {ply 0, e2e4}           │                                     │
  │ ← MOVE_MADE seq=4 re=3 {e4}          │ ← MOVE_MADE seq=4 {e4}              │
  │                                      │ → MOVE seq=4 {ply 1, e7e5}          │
  │ ← MOVE_MADE seq=5 {e5}               │ ← MOVE_MADE seq=5 re=4 {e5}         │
```

### 16.2 A dropped connection, resumed

```
Ana                                    Server                              Ben
  │ ✕ connection resets                  │                                     │
  │                                      │ (holds Ana's seat for 60 s)         │
  │                                      │ ← OPPONENT_STATUS {connected: false}│
  │ → HELLO seq=1 {resume_token: T1}     │                                     │
  │ ← WELCOME seq=1 re=1 {token: T2}     │                                     │
  │ ← STATE seq=2 {moves: [...]}         │ ← OPPONENT_STATUS {connected: true} │
  │ → MOVE seq=2 {ply n, ...}            │                                     │
  │ ← MOVE_MADE seq=3 re=2               │ ← MOVE_MADE                         │
```

### 16.3 A rejected move

```
Ben                                    Server
  │ → MOVE seq=5 {ply 1, e7e5}           │   (but white hasn't moved yet: ply is 0)
  │ ← MOVE_REJECTED seq=6 re=5 {code 302, "the move is for ply 1, but the game is at ply 0"}
```

### 16.4 A draw offer, accepted

```
Ana                                    Server                              Ben
  │ → OFFER_DRAW seq=8 {g1}              │                                     │
  │                                      │ ← DRAW_OFFERED {g1}                 │
  │                                      │ → ANSWER_DRAW {accept: true}        │
  │ ← GAME_OVER {1/2-1/2, agreement}     │ ← GAME_OVER {1/2-1/2, agreement}    │
```

## 17. A frame, byte by byte

The `MOVE` for `e2e4` as the third message on a connection, captured as it crosses the network. The
first four bytes are the length, 0x55 = 85, and the 85 bytes after them are the JSON payload.

```text hexdump
0000  00 00 00 55 7b 22 76 22 3a 31 2c 22 74 79 70 65  ...U{"v":1,"type
0010  22 3a 22 4d 4f 56 45 22 2c 22 73 65 71 22 3a 33  ":"MOVE","seq":3
0020  2c 22 72 65 22 3a 6e 75 6c 6c 2c 22 62 6f 64 79  ,"re":null,"body
0030  22 3a 7b 22 67 61 6d 65 5f 69 64 22 3a 22 67 31  ":{"game_id":"g1
0040  22 2c 22 70 6c 79 22 3a 30 2c 22 6d 6f 76 65 22  ","ply":0,"move"
0050  3a 22 65 32 65 34 22 7d 7d                       :"e2e4"}}
```

The reference implementation writes JSON with no spaces and its keys in the order `v`, `type`,
`seq`, `re`, `body`. Receivers **MUST NOT** depend on either: spacing and key order are free in JSON.

## 18. Constants

| Constant | Value |
|---|---|
| Protocol version | 1 |
| Default port | 5328 |
| Largest frame payload | 65,536 bytes |
| Heartbeat interval (client) | 10 seconds |
| Idle timeout (both sides) | 30 seconds |
| Resume window | 60 seconds |
| Name length | 1 to 32 characters |
