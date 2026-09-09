import { describe, expect, it } from "vitest";
import { kindLabel, localize, t } from "../src/core/i18n";

describe("localize", () => {
  it("на ru всегда возвращает первый аргумент, даже если есть en-вариант", () => {
    expect(localize("Иванов", "Ivanov", "ru")).toBe("Иванов");
  });

  it("на en возвращает второй аргумент, если он есть", () => {
    expect(localize("Иванов", "Ivanov", "en")).toBe("Ivanov");
  });

  it("на en остаётся на ru-варианте, если en-варианта нет (undefined или пустая строка)", () => {
    expect(localize("Иванов", undefined, "en")).toBe("Иванов");
    expect(localize("Иванов", "", "en")).toBe("Иванов");
  });
});

describe("t", () => {
  it("возвращает разные строки для ru и en по одному и тому же ключу", () => {
    expect(t("tab.authors", "ru")).toBe("Авторы");
    expect(t("tab.authors", "en")).toBe("Authors");
  });
});

describe("kindLabel", () => {
  it("собирает ключ kind.<вид> и возвращает нужный перевод", () => {
    expect(kindLabel("author", "ru")).toBe("Автор");
    expect(kindLabel("dept", "en")).toBe("Department");
  });
});
