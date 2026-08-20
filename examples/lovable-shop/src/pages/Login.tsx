import { useState } from "react";
import { supabase } from "@/integrations/supabase/client";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const signIn = async () => {
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) throw error;
  };

  return <form onSubmit={signIn}><input value={email} onChange={(e) => setEmail(e.target.value)} /></form>;
}
