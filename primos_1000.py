#!/usr/bin/env python3
"""Calcula y muestra los primeros 1000 numeros primos."""


def es_primo(n: int) -> bool:
    """Retorna True si n es primo."""
    if n < 2:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False

    divisor = 3
    while divisor * divisor <= n:
        if n % divisor == 0:
            return False
        divisor += 2

    return True


def generar_primos(cantidad: int) -> list[int]:
    """Genera una lista con la cantidad solicitada de numeros primos."""
    primos = []
    numero = 2

    while len(primos) < cantidad:
        if es_primo(numero):
            primos.append(numero)
        numero += 1

    return primos


if __name__ == "__main__":
    primos = generar_primos(1000)

    print("Primeros 1000 numeros primos:")
    for i, primo in enumerate(primos, start=1):
        print(f"{i:4d}: {primo}")

    print(f"\nTotal: {len(primos)} primos")
    print(f"Ultimo primo calculado: {primos[-1]}")
