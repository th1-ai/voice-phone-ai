# Property facts - Hotel Aurora (fixture data)

Sample data for `make demo` and the tests. A shorter copy of the same
invented property described in `knowledge/property.example.md`.

- Name: Hotel Aurora, 1 Example Street, 1000-001 Lisbon, Portugal
- Check-in from 15:00. Check-out by 11:00.
- The Aurora Kitchen (restaurant): open 19:00-22:30, closed Mondays.
- Room service (in-room dining): 07:00-22:00, EUR 8 tray charge.
- Room types: Classic Room, Sea View Room, Junior Suite, Hotel Aurora Suite -
  see `config/agent.example.yaml: rooms.room_types` for rates and counts.

`reservations.json` seeds a handful of confirmed bookings so
`tools/booking.py:preview_room`'s genuine availability check
(docs/how-it-works.md design decision 6) has something real to count
against in the demo - including one room type sold out for a specific stay,
so `make demo` shows both a normal quote and a "fully booked" reply.
