import { useParams } from "react-router-dom";
import OrderActions from "@/components/OrderActions";
import { supabase } from "@/integrations/supabase/client";

export default function OrderDetail() {
  const { id } = useParams();

  const load = async () => {
    const { data } = await supabase.from("orders").select("*").eq("id", id).single();
    return data;
  };

  return <div><OrderActions orderId={id!} /></div>;
}
