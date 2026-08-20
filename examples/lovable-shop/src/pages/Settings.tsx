import { useProfile } from "@/hooks/useProfile";

export default function Settings() {
  const { profile, save } = useProfile();
  return <form onSubmit={save}>{profile?.name}</form>;
}
