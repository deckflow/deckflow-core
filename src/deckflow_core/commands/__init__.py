"""Command implementations.

Core registers four: `env`, `auth`, `parse` and `update`.  Deck editing and
PPTX export are not brokered here — the Skill calls `@deckflow/html-editor` and
`@deckflow/deckhtml` directly, so their contracts stay with the tools that own
them instead of being restated in a second place.
"""
