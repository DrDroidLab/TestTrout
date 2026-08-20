import { supabase } from "@/integrations/supabase/client";

export default function OrderActions({ orderId }: { orderId: string }) {
  const refund = async () => {
    await supabase.from("payments").insert({ order_id: orderId, amount: 0, kind: "refund" });
    await supabase.from("orders").update({ status: "refunded" }).eq("id", orderId);
  };

  const remove = async () => {
    await supabase.from("orders").delete().eq("id", orderId);
  };

  const recalc = async () => {
    await supabase.rpc("recalculate_order_total", { order_id: orderId });
  };

  return <div><button onClick={refund}>Refund</button><button onClick={remove}>Delete</button></div>;
}
