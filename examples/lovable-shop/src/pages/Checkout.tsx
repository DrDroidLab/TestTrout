import { loadStripe } from "@stripe/stripe-js";
import { supabase } from "@/integrations/supabase/client";

const stripePromise = loadStripe(import.meta.env.VITE_STRIPE_KEY);

export default function Checkout() {
  const pay = async (orderId: string, amount: number) => {
    const stripe = await stripePromise;
    await supabase.from("payments").insert({ order_id: orderId, amount, kind: "charge" });
    await supabase.from("orders").update({ status: "paid" }).eq("id", orderId);
    return stripe;
  };

  return <button onClick={() => pay("", 0)}>Pay</button>;
}
