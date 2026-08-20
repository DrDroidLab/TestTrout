import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";

export default function OrderList() {
  const [orders, setOrders] = useState([]);

  useEffect(() => {
    const load = async () => {
      const { data } = await supabase
        .from("orders")
        .select("id, total, status, customer:profiles(name)")
        .eq("status", "open")
        .order("created_at")
        .limit(50);
      setOrders(data ?? []);
    };
    load();
  }, []);

  return <ul>{orders.map((o: any) => <li key={o.id}>{o.total}</li>)}</ul>;
}
