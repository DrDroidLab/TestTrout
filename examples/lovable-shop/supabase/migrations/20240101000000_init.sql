create table if not exists profiles (
  id uuid primary key references auth.users(id),
  name text not null,
  email text not null,
  created_at timestamptz default now()
);

create table if not exists orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id),
  total numeric(10,2) not null default 0,
  status text not null default 'open',
  created_at timestamptz default now()
);

create table if not exists payments (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null references orders(id),
  amount numeric(10,2) not null,
  kind text not null
);

create table if not exists audit_log (
  id bigserial primary key,
  actor uuid,
  action text not null
);

alter table profiles enable row level security;
alter table orders enable row level security;
alter table payments enable row level security;

create policy "Users read own profile" on profiles
  for select to authenticated
  using (auth.uid() = id);

create policy "Users update own profile" on profiles
  for update to authenticated
  using (auth.uid() = id)
  with check (auth.uid() = id);

create policy "Users manage own orders" on orders
  for all to authenticated
  using (auth.uid() = user_id);

create policy "Payments readable by order owner" on payments
  for select to authenticated
  using (exists (select 1 from orders o where o.id = payments.order_id and o.user_id = auth.uid()));
