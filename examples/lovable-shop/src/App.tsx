import { BrowserRouter, Route, Routes } from "react-router-dom";
import Login from "@/pages/Login";
import Orders from "@/pages/Orders";
import OrderDetail from "@/pages/OrderDetail";
import Checkout from "@/pages/Checkout";
import Settings from "@/pages/Settings";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/orders" element={<Orders />} />
        <Route path="/orders/:id" element={<OrderDetail />} />
        <Route path="/checkout" element={<Checkout />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </BrowserRouter>
  );
}
