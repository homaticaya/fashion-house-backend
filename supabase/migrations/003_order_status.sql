-- New order states. Kept in its own migration because a new enum value cannot be used in the transaction that adds it.
alter type order_status add value if not exists 'refund_pending';
alter type order_status add value if not exists 'refunded';
