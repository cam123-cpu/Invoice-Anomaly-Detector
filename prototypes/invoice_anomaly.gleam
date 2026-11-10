import gleam/dict
import gleam/int
import gleam/list

pub type Invoice {
  Invoice(id: String, vendor: String, amount: Float, date: String)
}

pub type Anomaly {
  Duplicate(id: String, of: String)
  Outlier(id: String, amount: Float, vendor_avg: Float)
}

/// Pure function: same invoices in, same anomalies out. No hidden state.
pub fn detect(invoices: List(Invoice)) -> List(Anomaly) {
  list.append(find_duplicates(invoices), find_outliers(invoices))
}

/// Same vendor + amount + date seen before => duplicate.
/// The "seen" dict is threaded through the fold, never mutated.
fn find_duplicates(invoices: List(Invoice)) -> List(Anomaly) {
  let #(_, found) =
    list.fold(invoices, #(dict.new(), []), fn(acc, inv) {
      let #(seen, found) = acc
      let key = #(inv.vendor, inv.amount, inv.date)
      case dict.get(seen, key) {
        Ok(first_id) -> #(seen, [Duplicate(inv.id, first_id), ..found])
        Error(Nil) -> #(dict.insert(seen, key, inv.id), found)
      }
    })
  list.reverse(found)
}

fn vendor_averages(invoices: List(Invoice)) -> dict.Dict(String, Float) {
  invoices
  |> list.group(fn(inv) { inv.vendor })
  |> dict.map_values(fn(_, group) {
    let total = list.fold(group, 0.0, fn(sum, inv) { sum +. inv.amount })
    total /. int.to_float(list.length(group))
  })
}

/// Amount more than 3x the vendor's average => outlier.
fn find_outliers(invoices: List(Invoice)) -> List(Anomaly) {
  let avgs = vendor_averages(invoices)
  list.filter_map(invoices, fn(inv) {
    case dict.get(avgs, inv.vendor) {
      Ok(avg) if inv.amount >. avg *. 3.0 ->
        Ok(Outlier(inv.id, inv.amount, avg))
      _ -> Error(Nil)
    }
  })
}

pub fn main() {
  let invoices = [
    Invoice("A1", "Acme", 100.0, "2026-10-01"),
    Invoice("A2", "Acme", 110.0, "2026-10-08"),
    Invoice("A3", "Acme", 100.0, "2026-10-01"),
    Invoice("A4", "Acme", 105.0, "2026-10-15"),
    Invoice("A5", "Acme", 95.0, "2026-10-22"),
    Invoice("A6", "Acme", 2000.0, "2026-10-29"),
  ]
  echo detect(invoices)
}
