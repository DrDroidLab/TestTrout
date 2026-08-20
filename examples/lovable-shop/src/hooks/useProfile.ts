import { supabase } from "@/integrations/supabase/client";

export function useProfile() {
  const load = async (userId: string) => {
    const { data } = await supabase.from("profiles").select("id, name, email").eq("id", userId).single();
    return data;
  };

  const save = async (userId: string, name: string) => {
    await supabase.from("profiles").update({ name }).eq("id", userId);
  };

  const uploadAvatar = async (userId: string, file: File) => {
    await supabase.storage.from("avatars").upload(`${userId}.png`, file);
  };

  return { load, save, uploadAvatar, profile: null };
}
